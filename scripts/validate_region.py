"""Проверка файла(ов) области перед коммитом/PR.

Использование:
    python scripts/validate_region.py                        # все regions/*.json
    python scripts/validate_region.py regions/akmola.json     # один файл

Проверяет: JSON валиден, meta.registry заполнен и slug совпадает с именем
файла, все kind/subtype/type — из допустимых значений (schema.py — тот же
список, что использует server.py при правке с фронта), у рёбер from/to
ссылаются на существующие узлы, id узлов/рёбер уникальны, И — отдельно от
schema.validate_graph — что idPrefix не пересекается с СОСЕДНИМИ областями
(этого schema.py в одиночку не проверит: ему виден только один файл, а
коллизия обнаруживается сравнением со всеми regions/*.json сразу).

Код возврата: 0 — всё чисто, 1 — есть ошибки (для CI/pre-push hook).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))   # чтобы импортировался schema.py рядом с server.py

from schema import validate_graph  # noqa: E402


def collect_id_prefixes(all_files: list[Path]) -> dict[str, list[str]]:
    by_prefix: dict[str, list[str]] = {}
    for p in all_files:
        try:
            reg = json.loads(p.read_text(encoding="utf-8"))["meta"]["registry"]
        except Exception:  # noqa: BLE001 — свою ошибку файл получит через validate_graph
            continue
        by_prefix.setdefault(reg.get("idPrefix", ""), []).append(reg.get("slug", p.stem))
    return by_prefix


def main() -> None:
    args = sys.argv[1:]
    regions_dir = HERE.parent / "regions"
    all_files = sorted(regions_dir.glob("*.json"))
    targets = [Path(a) for a in args] if args else all_files

    if not all_files:
        sys.exit("regions/ пуст или не найден")

    prefixes = collect_id_prefixes(all_files)
    collisions = {p: slugs for p, slugs in prefixes.items() if len(slugs) > 1}

    ok = True
    for path in targets:
        if not path.exists():
            print(f"✗ {path}: файл не найден")
            ok = False
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"✗ {path}: битый JSON — {e}")
            ok = False
            continue

        errors = validate_graph(data, file_slug=path.stem)

        reg = (data.get("meta") or {}).get("registry") or {}
        pfx = reg.get("idPrefix")
        if pfx in collisions:
            others = [s for s in collisions[pfx] if s != reg.get("slug")]
            errors.append(f"idPrefix {pfx!r} также занят: {', '.join(others)} "
                          f"— выберите другой (scripts/new_region.py делает это "
                          f"автоматически)")

        if errors:
            print(f"✗ {path} — {len(errors)} ошибок:")
            for e in errors:
                print(f"    - {e}")
            ok = False
        else:
            n = len(data.get("nodes", []))
            e = len(data.get("edges", []))
            print(f"✓ {path} — {n} узлов, {e} рёбер, idPrefix={pfx!r}")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
