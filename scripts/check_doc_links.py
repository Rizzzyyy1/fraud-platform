"""Fail if a Markdown file links to a local file that does not exist.

Checks README.md and docs/**/*.md: relative link targets and image paths, ignoring URLs and
in-page anchors. Usage: python scripts/check_doc_links.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


def main() -> int:
    files = [Path("README.md"), *sorted(Path("docs").rglob("*.md"))]
    missing: list[str] = []
    checked = 0
    for file in files:
        for target in LINK.findall(file.read_text()):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = target.split("#", 1)[0].split(":", 1)[0]
            checked += 1
            if not (file.parent / path).exists():
                missing.append(f"{file}: {target}")
    print(f"checked {checked} local links in {len(files)} files")
    for m in missing:
        print("missing:", m)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
