"""The old product name appears only in compatibility code, its tests and the upgrade notes."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files that may mention the old name anywhere: the upgrade notes, the compatibility
# code that reads old names, and the tests that exercise that code.
ALLOWED_FILES = {
    "docs/UPGRADING-FROM-WATCHPOST.md",
    "observe/compat.py",
    "observe/plugins.py",
    "tests/test_name_guard.py",
    "tests/test_rename.py",
    "tests/test_rename_runtime.py",
    "tests/test_control_keys.py",
}
# Other files may mention it only on a line that says it is the old or legacy name.
MARKERS = ("legacy", "old name", "compat", "formerly")


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                         check=True).stdout.decode()
    return [p for p in out.split("\0") if p]


def test_old_name_is_confined_to_compatibility_code():
    pattern = re.compile("watchpost", re.IGNORECASE)
    offenders = []
    for rel in _tracked():
        if pattern.search(rel):
            if rel not in ALLOWED_FILES:
                offenders.append(f"{rel} (file name)")
            continue
        if rel in ALLOWED_FILES:
            continue
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line) and not any(m in line.lower() for m in MARKERS):
                offenders.append(f"{rel}:{number}")
    assert not offenders, offenders


def test_pages_title_says_observe():
    pages = [p for p in _tracked() if p.endswith(".html") and not p.startswith("tests/")]
    assert pages
    for rel in pages:
        text = (ROOT / rel).read_text(encoding="utf-8")
        match = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        if match:
            assert "observe" in match.group(1).lower(), rel
