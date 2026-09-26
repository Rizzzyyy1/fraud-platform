"""Analyst accounts and sessions for the console.

Accounts live in a local JSON file (default `run/dashboard/analysts.json`, git-ignored) holding
scrypt password hashes; nothing is stored in plain text and no account ships with the code.
Sessions are random 256-bit tokens kept in the console process's memory (a restart signs every
analyst out), sent as an HttpOnly, SameSite=Strict cookie. Every state-changing request must
also carry the session's CSRF token in `X-CSRF-Token`. Failed logins are throttled per username.

Create an account: `python -m fraudplat.console.auth add <name> --role analyst|admin`
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Role = Literal["analyst", "admin"]
SESSION_TTL_S = 8 * 3600
MAX_FAILURES = 5
FAILURE_WINDOW_S = 300
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hash_password(password, bytes.fromhex(salt_hex)).split("$")[2]
    return secrets.compare_digest(candidate, digest_hex)


@dataclass(frozen=True)
class Analyst:
    name: str
    role: Role


@dataclass(frozen=True)
class Session:
    analyst: Analyst
    csrf: str
    expires_at: float


class Accounts:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        data: dict[str, dict[str, str]] = json.loads(self.path.read_text())
        return data

    def add(self, name: str, password: str, role: Role) -> None:
        if not name or len(name) > 64 or not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("name must be 1-64 characters of letters, digits, '-' or '_'")
        if len(password) < 12:
            raise ValueError("password must be at least 12 characters")
        data = self._load()
        data[name] = {"role": role, "password": hash_password(password)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2) + "\n")
        self.path.chmod(0o600)

    def authenticate(self, name: str, password: str) -> Analyst | None:
        entry = self._load().get(name)
        if entry is None:
            hash_password(password)  # comparable work for unknown names
            return None
        if not verify_password(password, entry["password"]):
            return None
        role: Role = "admin" if entry["role"] == "admin" else "analyst"
        return Analyst(name, role)


class Sessions:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._failures: dict[str, list[float]] = {}

    def throttled(self, name: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        recent = [t for t in self._failures.get(name, []) if now - t < FAILURE_WINDOW_S]
        self._failures[name] = recent
        return len(recent) >= MAX_FAILURES

    def record_failure(self, name: str, now: float | None = None) -> None:
        self._failures.setdefault(name, []).append(time.monotonic() if now is None else now)

    def create(self, analyst: Analyst) -> tuple[str, Session]:
        self._failures.pop(analyst.name, None)
        token = secrets.token_urlsafe(32)
        session = Session(analyst, secrets.token_urlsafe(24), time.monotonic() + SESSION_TTL_S)
        self._sessions[token] = session
        return token, session

    def get(self, token: str | None) -> Session | None:
        if token is None:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= time.monotonic():
            del self._sessions[token]
            return None
        return session

    def revoke(self, token: str | None) -> None:
        if token is not None:
            self._sessions.pop(token, None)


def main() -> int:
    parser = argparse.ArgumentParser(description="manage local console accounts")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--role", choices=["analyst", "admin"], default="analyst")
    add.add_argument("--file", type=Path, default=Path("run/dashboard/analysts.json"))
    args = parser.parse_args()
    password = getpass.getpass(f"password for {args.name}: ")
    if password != getpass.getpass("repeat: "):
        raise SystemExit("passwords differ")
    Accounts(args.file).add(args.name, password, args.role)
    print(f"added {args.name} ({args.role}) to {args.file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
