"""The committed OpenAPI schema of /api/v2 (docs/openapi-v2.json).

`python -m observe.api.schema` writes the file, and `python -m observe.api.schema --check` exits
with 1 when the file differs from what the code generates, which is the CI check. A test does
the same, so a change to a route or a model cannot land without its schema change in the diff.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..alerts import Alerter
from ..config import Config
from ..scheduler import Scheduler
from ..store import Store
from . import ApiRuntime, build

PATH = Path(__file__).resolve().parents[2] / "docs" / "openapi-v2.json"


def generate() -> dict[str, Any]:
    """The schema of the core resources, built on an empty in-memory database."""
    config = Config.model_validate({"server": {"db_path": ":memory:"}, "monitors": []})
    store = Store(":memory:")
    try:
        runtime = ApiRuntime(config, store, scheduler=Scheduler(config, store, Alerter(config)),
                             alerter=Alerter(config))
        app, _ = build(runtime)
        return app.openapi()
    finally:
        store.close()


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    text = render(generate())
    if "--check" in argv:
        current = PATH.read_text(encoding="utf-8") if PATH.exists() else ""
        if current != text:
            print(f"{PATH} is out of date; run: python -m observe.api.schema", file=sys.stderr)
            return 1
        return 0
    PATH.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
