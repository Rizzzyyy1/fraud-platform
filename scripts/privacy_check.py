"""Report files that contain personal or sensitive information, without printing it.

    python scripts/privacy_check.py [--root DIR] [--terms-file FILE] [--git-history] [--json]

Scans the files of a checkout (`git ls-files` in a Git repository, otherwise every file except
dependency and build directories), including printable strings inside binary files (images,
video, databases). Each finding is reported as `path[:line]  category`; the matched text is never
printed. Exit status 1 if anything is found.

Categories:
  personal_term       a term from the local terms file (names, handles, personal emails); the
                      file lives outside the repository (default ~/.config/fraud-platform/
                      privacy-terms.txt or $PRIVACY_TERMS_FILE), one term per line
  home_path           /Users/<name>/, /home/<name>/ or C:\\Users\\<name>\\
  email               an email address that is not an obvious placeholder
  github_noreply      a GitHub no-reply address (identifies an account)
  private_key         a PEM private-key block
  token               common API-token formats (GitHub, OpenAI-style, AWS, Slack, JWT)
  private_ip          an RFC 1918 address
  host_name           a *.local host or a default laptop host name
  credential          an assignment of a non-placeholder value to a password/secret/key field
  sensitive_file      a file that should not be published (.env, databases, account files, logs)
With --git-history (Git repositories only), author and committer identities are checked too.

`--allow PATH:CATEGORY` (repeatable) marks a finding as an approved public disclosure, for example
the owner's name in LICENSE. Only identity categories (personal_term, github_noreply, email) can be
approved; home paths, credentials, tokens, keys, private IPs, host names and sensitive files can
never be. Approved findings are still listed, separately, and do not fail the check.

Limitations: pattern-based. Binary files are checked only through their printable byte runs
(metadata and text-like fragments), not by viewing images or video frames, and short
email-shaped fragments in binary data are ignored. Zero findings is not a guarantee of anonymity.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "node_modules", "dist", "__pycache__", ".mypy_cache",
             ".ruff_cache", ".pytest_cache", "run", "data", "artifacts", "mlruns",
             "mlartifacts", "media"}  # fmt: skip
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webm", ".mp4", ".db", ".sqlite", ".pdf"}
SENSITIVE_NAMES = re.compile(
    r"(^|/)(\.env|analysts\.json|pids\.json|worker\.hold|mlflow\.db)$|\.(db|sqlite|log|pem|key)$"
)
PLACEHOLDERS = {
    "change-me-local-only", "ci-only", "itest-only", "integration-test-key",
    "integration password", "correct horse battery", "long enough password",
}  # fmt: skip
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "home_path",
        re.compile(
            r"/(?:Users|home)/(?!<|\$|shared\b|runner\b)[A-Za-z0-9._-]+/"
            r"|[A-Za-z]:\\Users\\[^\\\s]+\\"
        ),
    ),
    ("github_noreply", re.compile(r"\b\d+\+[A-Za-z0-9-]+@users\.noreply\.github\.com\b")),
    (
        "email",
        re.compile(
            r"\b[A-Za-z0-9._%+-]+@"
            r"(?!example\.(?:com|org)\b|localhost\b|users\.noreply\.github\.com\b)"
            r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
        ),
    ),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "token",
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
            r"|xox[baprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})"
        ),
    ),
    (
        "private_ip",
        re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    ),
    (
        "host_name",
        re.compile(
            r"\b[A-Za-z0-9-]+\.local\b|\b[A-Za-z]+s-(?:MacBook|iMac|Mac-mini)[A-Za-z0-9-]*\b"
        ),
    ),
]
# A literal value assigned to a password/secret/key/token field: `KEY=value` lines (.env style)
# or a quoted string in code/config. Variables, annotations and lookups are not literals.
_FIELD = r"[A-Za-z0-9_]*(?:PASSWORD|SECRET|API_?KEY|TOKEN)[A-Za-z0-9_]*"
CREDENTIAL_ENV = re.compile(rf"^\s*{_FIELD.upper()}=([^\s#]{{6,}})\s*$")
CREDENTIAL_QUOTED = re.compile(rf"(?i)\b{_FIELD}\s*[=:]\s*[\"']([^\"']{{6,}})[\"']")


def load_terms(path: Path | None) -> list[str]:
    if path is None or not path.exists():
        return []
    return [t.strip() for t in path.read_text().splitlines() if t.strip() and not t.startswith("#")]


def list_files(root: Path) -> list[Path]:
    if (root / ".git").exists():
        out = subprocess.run(  # noqa: S603 - fixed argv
            ["git", "-C", str(root), "ls-files", "-z"],  # noqa: S607
            capture_output=True,
            check=True,
        ).stdout.decode()
        return [root / p for p in out.split("\0") if p and (root / p).is_file()]
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        files.extend(Path(dirpath) / f for f in filenames)
    return files


def printable_strings(data: bytes, minimum: int = 6) -> str:
    return "\n".join(m.decode("ascii") for m in re.findall(rb"[\x20-\x7e]{%d,}" % minimum, data))


def scan_text(text: str, terms: list[str], binary: bool = False) -> list[tuple[int, str]]:
    """In binary files, email-shaped runs shorter than 12 characters are ignored: compressed
    media produces short random matches (one or two characters on each side of an at sign)."""
    found: list[tuple[int, str]] = []
    lowered = [t.lower() for t in terms]
    for number, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        if any(t in low for t in lowered):
            found.append((number, "personal_term"))
        for category, pattern in PATTERNS:
            match = pattern.search(line)
            if match and not (binary and category == "email" and len(match.group(0)) < 12):
                found.append((number, category))
        for rule in (CREDENTIAL_ENV, CREDENTIAL_QUOTED):
            match = rule.search(line)
            if match and match.group(1) not in PLACEHOLDERS:
                found.append((number, "credential"))
                break
    return found


def scan(root: Path, terms: list[str]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for path in sorted(list_files(root)):
        rel = path.relative_to(root).as_posix()
        if SENSITIVE_NAMES.search(rel):
            findings.append({"path": rel, "line": None, "category": "sensitive_file"})
        data = path.read_bytes()
        binary = path.suffix.lower() in BINARY_SUFFIXES or b"\0" in data[:4096]
        text = printable_strings(data) if binary else data.decode("utf-8", "replace")
        for line, category in scan_text(text, terms, binary):
            findings.append({"path": rel, "line": None if binary else line, "category": category})
    return findings


def scan_history(root: Path, terms: list[str]) -> list[dict[str, object]]:
    out = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(root), "log", "--all", "--format=%an%n%ae%n%cn%n%ce"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    identities = set(out.splitlines())
    categories = {c for _, c in scan_text("\n".join(identities), terms)}
    where = "<git author/committer metadata>"
    return [{"path": where, "line": None, "category": c} for c in sorted(categories)]


ALLOWABLE = {"personal_term", "github_noreply", "email"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=Path())
    default_terms = os.environ.get("PRIVACY_TERMS_FILE") or str(
        Path.home() / ".config/fraud-platform/privacy-terms.txt"
    )
    parser.add_argument("--terms-file", type=Path, default=Path(default_terms))
    parser.add_argument("--git-history", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow", action="append", default=[], metavar="PATH:CATEGORY")
    args = parser.parse_args()
    allowed: set[tuple[str, str]] = set()
    for item in args.allow:
        path, _, category = item.rpartition(":")
        if category not in ALLOWABLE:
            parser.error(f"--allow cannot approve {category!r}; only {sorted(ALLOWABLE)}")
        allowed.add((path, category))
    root = args.root.resolve()
    terms = load_terms(args.terms_file)
    findings = scan(root, terms)
    if args.git_history and (root / ".git").exists():
        findings += scan_history(root, terms)
    approved = [f for f in findings if (f["path"], f["category"]) in allowed]
    findings = [f for f in findings if (f["path"], f["category"]) not in allowed]
    summary = {
        "files_scanned": len(list_files(root)),
        "personal_terms_loaded": len(terms),
        "approved_disclosures": approved,
        "findings": findings,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"scanned {summary['files_scanned']} files; {len(terms)} personal terms loaded")
        if not terms:
            print("note: no personal terms file; only generic patterns were checked")
        for f in approved:
            where = f"{f['path']}:{f['line']}" if f["line"] else str(f["path"])
            print(f"{where}  {f['category']}  (approved public disclosure)")
        for f in findings:
            where = f"{f['path']}:{f['line']}" if f["line"] else str(f["path"])
            print(f"{where}  {f['category']}")
        print("no unapproved findings" if not findings else f"{len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
