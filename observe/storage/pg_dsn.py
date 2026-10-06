"""Checks on the PostgreSQL connection string and the secret file holding its password.

The string is a URI (`postgresql://observe@db:5432/observe`) or keywords (`host=db dbname=observe
user=observe`). It never carries the password: that is read from `storage.password_file`, so the
string can sit in a config file and in a backup without exposing a secret. No message here
repeats a value from the string or the file.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

_KEYWORD_PASSWORD = re.compile(r"(?:^|\s)password\s*=", re.IGNORECASE)
_SCHEME = re.compile(r"^postgres(?:ql)?://", re.IGNORECASE)


def check_dsn(dsn: str) -> str:
    """Return the string, or raise ValueError when it is empty or carries a password."""
    text = dsn.strip()
    if not text:
        raise ValueError("storage.dsn is empty")
    if _SCHEME.match(text):
        try:
            parts = urlsplit(text)
            queries = [k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)]
            has = parts.password is not None or "password" in queries
        except ValueError:
            raise ValueError("storage.dsn is not a valid PostgreSQL URI") from None
    else:
        has = bool(_KEYWORD_PASSWORD.search(text))
    if has:
        raise ValueError("storage.dsn must not contain a password; put it in the file named by "
                         "storage.password_file")
    return text


def read_password(path: str) -> str:
    """The password in the file, without surrounding whitespace. Raises OSError or ValueError
    without the file's contents in the message."""
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"the password file {path} is empty")
    return text
