"""Deterministic JSON export of the demo catalog (v0.6.1 handoff artifact).

Non-Python frontends can consume ``src/demo/data/demo_catalog.json`` directly
or regenerate it with ``python -m src.demo.export <output.json>``. The export
is a pure projection of the Python fixtures: a committed snapshot that no
longer regenerates byte-identically fails the focused tests, so the two
representations cannot drift apart.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from src.demo.catalog import DemoCatalog, load_demo_catalog
from src.demo.contracts import DEMO_MODE, DEMO_VERSION
from src.demo.fixtures import DEMO_KB_UUID

_HONESTY_NOTE = (
    "预置离线演示数据（mock_demo）：不是真实 AI 输出，不是已批准比赛语料，"
    "不包含真实生产资料。integrity_state/demo_note 为演示预置状态，"
    "不是实时核验结果。"
)


def catalog_payload(catalog: DemoCatalog | None = None) -> dict[str, object]:
    """Project the catalog into a JSON-serializable, order-stable payload."""
    catalog = catalog if catalog is not None else load_demo_catalog()
    return {
        "demo_version": DEMO_VERSION,
        "mode": DEMO_MODE,
        "kb_uuid": DEMO_KB_UUID,
        "honesty_note": _HONESTY_NOTE,
        "presets": [asdict(preset) for preset in catalog.presets],
        "sources": [asdict(source) for source in catalog.sources],
        "responses": {
            fixture.key: fixture.response.model_dump(mode="json")
            for fixture in catalog.responses
        },
    }


def catalog_to_json(catalog: DemoCatalog | None = None, *, indent: int = 2) -> str:
    """Return the deterministic JSON document (trailing newline included)."""
    return json.dumps(
        catalog_payload(catalog), ensure_ascii=False, indent=indent
    ) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Write the snapshot to a file, or print it when no path is given."""
    parser = argparse.ArgumentParser(description="导出比赛演示 mock fixtures JSON")
    parser.add_argument("output", nargs="?", help="输出 JSON 路径；缺省打印到 stdout")
    args = parser.parse_args(argv)
    document = catalog_to_json()
    if args.output is None:
        sys.stdout.write(document)
        return 0
    Path(args.output).write_text(document, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
