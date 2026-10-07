"""The old product name appears nowhere in the repository except this guard."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The guard itself has to spell the name it forbids.
ALLOWED_FILES = {"tests/test_name_guard.py"}
OLD_NAME = "watch" + "post"


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                         check=True).stdout.decode()
    return [p for p in out.split(chr(0)) if p and (ROOT / p).exists()]


def test_old_name_is_gone():
    pattern = re.compile(OLD_NAME, re.IGNORECASE)
    offenders = []
    for rel in _tracked():
        if rel in ALLOWED_FILES:
            continue
        if pattern.search(rel):
            offenders.append(f"{rel} (file name)")
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        offenders += [f"{rel}:{n}" for n, line in enumerate(text.splitlines(), 1)
                      if pattern.search(line)]
    assert not offenders, offenders


def test_compat_module_is_gone():
    assert not (ROOT / "observe" / "compat.py").exists()


def test_pages_title_says_observe():
    pages = [p for p in _tracked() if p.endswith(".html") and not p.startswith("tests/")]
    assert pages
    for rel in pages:
        text = (ROOT / rel).read_text(encoding="utf-8")
        match = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        if match:
            assert "observe" in match.group(1).lower(), rel
