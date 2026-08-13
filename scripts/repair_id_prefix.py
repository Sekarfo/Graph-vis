"""Сменить idPrefix области ВМЕСТЕ С id всех её узлов.

Зачем: idPrefix обязан быть уникален среди всех regions/*.json — server.py
при обнаружении графов молча ПРОПУСКАЕТ область, чей префикс уже занят
(discover_graphs()), потому что при совпадении префиксов id узлов двух
областей пересекаются, а привязка отчёта от бота (graph_from_node/
graph_to_node) работает по принципу «оба id есть в этом графе» — коллизия
увела бы километры в чужую область.

Сменить одну строку meta.registry.idPrefix НЕДОСТАТОЧНО: id уже созданных
узлов начинаются со старой буквы, и пересечение никуда не денется. Этот
скрипт переписывает префикс во ВСЕХ ссылках на узлы внутри файла:
nodes[].id, edges[].from/to, externalLinks[].from/to, serviceLinks[].from/to
и строках serviceLinksHidden («<from>|<to>»).

НЕ трогает id рёбер (они начинаются с «E») и id сервисных линий («S», «svc:»):
они локальны для файла и между областями не сравниваются.

Использование:
    python scripts/repair_id_prefix.py kyzylorda Q
    python scripts/repair_id_prefix.py kyzylorda Q --dry-run

После правки прогоните scripts/validate_region.py и scripts/build_fallback.py
(или запустите с --rebuild-fallback, что делает это сразу).
"""
from __future__ import annotations

import argparse
import json
import string
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGIONS_DIR = HERE.parent / "regions"

# Буквы, занятые НЕ узлами: id рёбер начинаются с «E», псевдоузлы внешних
# связей — с «X». Отдать их узлам нельзя: schema.validate_graph справедливо
# ругается, когда id ребра совпадает с id узла.
RESERVED = {"E", "X"}


def load(slug: str) -> tuple[Path, dict]:
    path = REGIONS_DIR / f"{slug}.json"
    if not path.exists():
        sys.exit(f"нет файла regions/{slug}.json")
    return path, json.loads(path.read_text(encoding="utf-8"))


def taken_prefixes(except_slug: str) -> dict[str, str]:
    taken: dict[str, str] = {}
    for p in REGIONS_DIR.glob("*.json"):
        try:
            reg = json.loads(p.read_text(encoding="utf-8"))["meta"]["registry"]
        except Exception:  # noqa: BLE001
            continue
        if reg.get("slug") != except_slug:
            taken[reg["idPrefix"]] = reg["slug"]
    return taken


def rename_ids(data: dict, old: str, new: str) -> tuple[dict, int]:
    """Заменить ведущий префикс во всех ссылках на узлы. Возвращает
    (файл с новыми id, сколько id переименовано)."""
    ids = {n["id"] for n in data.get("nodes", [])}
    mapping = {i: new + i[len(old):] for i in ids if i.startswith(old)}
    if len(mapping) != len(ids):
        skipped = sorted(ids - set(mapping))[:5]
        sys.exit(f"не все id узлов начинаются с {old!r} (например {skipped}) — "
                 f"файл придётся править вручную")
    collide = set(mapping.values()) & (ids - set(mapping))
    if collide:
        sys.exit(f"новые id пересеклись бы со старыми: {sorted(collide)[:5]}")

    def m(v):
        return mapping.get(v, v)

    for n in data.get("nodes", []):
        n["id"] = m(n["id"])
    for e in data.get("edges", []):
        e["from"], e["to"] = m(e["from"]), m(e["to"])
    for key in ("externalLinks", "serviceLinks", "serviceLinksAuto", "serviceLinksOff"):
        for l in data.get(key) or []:
            if isinstance(l, dict):
                for fld in ("from", "to"):
                    if fld in l:
                        l[fld] = m(l[fld])
    hidden = data.get("serviceLinksHidden")
    if isinstance(hidden, list):
        data["serviceLinksHidden"] = [
            "|".join(m(part) for part in h.split("|")) if isinstance(h, str) else h
            for h in hidden
        ]
    return data, len(mapping)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug", help="область, у которой меняем префикс (напр. kyzylorda)")
    ap.add_argument("new_prefix", nargs="?", help="новая буква; по умолчанию — первая свободная")
    ap.add_argument("--dry-run", action="store_true", help="показать, что будет сделано")
    ap.add_argument("--rebuild-fallback", action="store_true",
                    help="сразу перегенерировать regions/<slug>.fallback.js")
    args = ap.parse_args()

    path, data = load(args.slug)
    reg = data["meta"]["registry"]
    old = reg["idPrefix"]
    taken = taken_prefixes(args.slug)

    new = args.new_prefix
    if not new:
        new = next((c for c in string.ascii_uppercase
                    if c not in taken and c not in RESERVED), None)
        if new is None:
            sys.exit("не осталось свободных однобуквенных префиксов — задайте вручную")
    new = new.upper()
    if new in taken:
        sys.exit(f"префикс {new!r} уже занят областью {taken[new]!r}")
    if new in RESERVED:
        sys.exit(f"префикс {new!r} зарезервирован (E — id рёбер, X — псевдоузлы "
                 f"внешних связей); выберите другую букву")
    if new == old:
        sys.exit(f"префикс уже {old!r} — менять нечего")

    data, n = rename_ids(data, old, new)
    reg["idPrefix"] = new

    print(f"{args.slug}: idPrefix {old!r} → {new!r}, переименовано узлов: {n}")
    if args.dry_run:
        print("(--dry-run — файл не тронут)")
        return

    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"записано: regions/{path.name}")

    if args.rebuild_fallback:
        subprocess.run([sys.executable, str(HERE / "build_fallback.py"), str(path)], check=True)


if __name__ == "__main__":
    main()
