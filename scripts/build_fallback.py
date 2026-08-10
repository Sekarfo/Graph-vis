"""Перегенерировать regions/<slug>.fallback.js из regions/<slug>.json —
для просмотра через file:// (двойной клик по index.html) без сервера.

Через server.py фоллбэк пересоздаётся сам при каждой правке с фронта;
этот скрипт нужен, только если вы правили JSON руками в редакторе (не через
браузер) и хотите, чтобы file:// сразу видел изменения.

Использование:
    python scripts/build_fallback.py                       # все regions/*.json
    python scripts/build_fallback.py regions/akmola.json    # один файл
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGIONS_DIR = HERE.parent / "regions"


def build_one(json_path: Path) -> None:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    slug = json_path.stem
    js_path = json_path.with_suffix("").with_suffix(".fallback.js")
    compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    js_path.write_text(
        "window.GRAPH_DATA_BY_SLUG = window.GRAPH_DATA_BY_SLUG || {};\n"
        f"window.GRAPH_DATA_BY_SLUG[{json.dumps(slug)}] = {compact};",
        encoding="utf-8",
    )
    print(f"regions/{js_path.name} <- regions/{json_path.name}")


def main() -> None:
    args = sys.argv[1:]
    targets = [Path(a) for a in args] if args else sorted(REGIONS_DIR.glob("*.json"))
    for p in targets:
        build_one(p)


if __name__ == "__main__":
    main()
