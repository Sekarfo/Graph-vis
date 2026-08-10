"""Создаёт скелет новой области: regions/<slug>.json + пустой fallback-файл.

Использование:
    python scripts/new_region.py <slug> "<Название области>" [--id-prefix XX]

<slug> — латиницей, только [a-z0-9-], без пробелов (используется в URL:
?graph=<slug>) и в имени файла. idPrefix подбирается автоматически (первая
свободная буква/пара латинских букв, ещё не занятая соседними regions/*.json)
и ОБЯЗАН быть уникален среди всех областей — сервер откажется грузить область
с уже занятым префиксом (см. discover_graphs() в server.py).

После создания файла: следующие шаги — в CONTRIBUTING.md.
"""
from __future__ import annotations

import argparse
import json
import re
import string
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGIONS_DIR = HERE.parent / "regions"

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

SKELETON_META = {
    "schema": {
        "nodes": "id; kind — тип узла (ats|olt|atn|netengine|mufta|snp); subtype — подтип (для оборудования: existing|planned|outside; для СНП: fiber|wifi|starlink|outside); name — название; district — район; connectionCount — число планируемых подключений (для СНП); x/y — координаты раскладки редактора; objectId — id объекта во внешней системе (если сопоставлен)",
        "edges": "id; from/to — id узлов; type — тип ВОЛС (см. legend); lengthKm — плановая длина сегмента с чертежа; progress — ход строительства (если задан)",
        "externalLinks": "рёбра, выходящие за границу области; дальний конец описан в externalEndpoint",
    },
    "legend": {
        "nodeKinds": {
            "ats": "АТС — существующая телефонная станция (головной узел)",
            "olt": "OLT — оптический линейный терминал",
            "atn": "ATN — транзитный узел на маршруте (село-коннектор)",
            "netengine": "NetEngine — транспортный узел",
            "mufta": "муфта — оптическая муфта (name = номер муфты)",
            "snp": "СНП — сельский населённый пункт для подключения",
        },
        "snpTech": {
            "fiber": "СНП по проекту (подключается оптикой)",
            "outside": "СНП вне проекта",
            "wifi": "СНП по технологии Wi-Fi public",
            "starlink": "СНП по технологии Спутник (Starlink)",
        },
        "edgeTypes": {
            "existing": "существующая ВОЛС",
            "planned": "ВОЛС по проекту (к строительству)",
            "outside_project": "ВОЛС вне проекта",
            "no_free_fibers": "существующая ВОЛС без свободных ОВ",
            "larger_capacity": "ВОЛС по проекту большей ёмкости",
        },
    },
    "counts": {
        "nodes": 0, "nodesByKind": {}, "snpByTech": {}, "districts": 0,
        "edges": 0, "edgesByType": {}, "externalLinks": 0,
        "totalKm": 0, "plannedKm": 0,
    },
}


def taken_prefixes() -> dict[str, str]:
    taken = {}
    for p in REGIONS_DIR.glob("*.json"):
        try:
            reg = json.loads(p.read_text(encoding="utf-8"))["meta"]["registry"]
            taken[reg["idPrefix"]] = reg["slug"]
        except Exception:  # noqa: BLE001
            continue
    return taken


def pick_id_prefix(taken: dict[str, str]) -> str:
    for ch in string.ascii_uppercase:
        if ch not in taken:
            return ch
    for a in string.ascii_uppercase:
        for b in string.ascii_uppercase:
            if a + b not in taken:
                return a + b
    raise RuntimeError("не осталось свободных префиксов — задайте --id-prefix вручную")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug", help="латиницей, напр. akmola, sko, pavlodar")
    ap.add_argument("title", help="человекочитаемое название области")
    ap.add_argument("--id-prefix", help="буква(-ы) для id новых узлов; по умолчанию — "
                                        "первая свободная")
    args = ap.parse_args()

    if not SLUG_RE.match(args.slug):
        sys.exit(f"slug {args.slug!r} должен быть латиницей: [a-z0-9]+(-[a-z0-9]+)* "
                 f"(например: akmola, west-kazakhstan)")

    REGIONS_DIR.mkdir(exist_ok=True)
    out_path = REGIONS_DIR / f"{args.slug}.json"
    if out_path.exists():
        sys.exit(f"regions/{args.slug}.json уже существует")

    taken = taken_prefixes()
    id_prefix = args.id_prefix or pick_id_prefix(taken)
    if id_prefix in taken:
        sys.exit(f"idPrefix {id_prefix!r} уже занят областью {taken[id_prefix]!r} "
                 f"(regions/{taken[id_prefix]}.json) — выберите другой через --id-prefix")

    data = {
        "meta": {
            "registry": {"slug": args.slug, "title": args.title, "idPrefix": id_prefix},
            "region": args.title,
            **SKELETON_META,
            "status": "КАРКАС: нет данных, ждёт оцифровки",
        },
        "nodes": [],
        "edges": [],
        "externalLinks": [],
    }
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    js_path = REGIONS_DIR / f"{args.slug}.fallback.js"
    js_path.write_text(
        "window.GRAPH_DATA_BY_SLUG = window.GRAPH_DATA_BY_SLUG || {};\n"
        f"window.GRAPH_DATA_BY_SLUG[{json.dumps(args.slug)}] = "
        + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";",
        encoding="utf-8",
    )

    print(f"Создано: regions/{args.slug}.json (idPrefix={id_prefix!r})")
    print(f"Создано: regions/{args.slug}.fallback.js")
    print()
    print("Дальше (см. CONTRIBUTING.md):")
    print(f"  1. uvicorn server:app --reload")
    print(f"  2. http://localhost:8000/index.html?graph={args.slug}&edit=1")
    print(f"  3. Наполняйте узлами/связями через ➕ Объект / 🔗 Связь "
         f"(или своим скриптом оцифровки — правки всё равно сохраняются в этот файл)")
    print(f"  4. python scripts/validate_region.py regions/{args.slug}.json")
    print(f"  5. git add regions/{args.slug}.json regions/{args.slug}.fallback.js "
         f"&& git commit && PR")


if __name__ == "__main__":
    main()
