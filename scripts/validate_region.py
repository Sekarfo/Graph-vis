"""Проверка файла(ов) области перед коммитом/PR.

Использование:
    python scripts/validate_region.py                        # все regions/*.json
    python scripts/validate_region.py regions/akmola.json     # один файл

Проверяет: JSON валиден, meta.registry заполнен и slug совпадает с именем
файла, все kind/subtype/type — из допустимых значений (schema.py — тот же
список, что использует server.py при правке с фронта), у рёбер from/to
ссылаются на существующие узлы, id узлов/рёбер уникальны, И — отдельно от
schema.validate_graph — три МЕЖФАЙЛОВЫЕ проверки (этого schema.py в одиночку
не сделает: ему виден только один файл, а коллизия обнаруживается сравнением
со всеми regions/*.json сразу):

  * idPrefix не пересекается с СОСЕДНИМИ областями;
  * ни один id узла не встречается в другой области — это то, ради чего
    и нужен уникальный idPrefix: привязка отчёта от бота (graph_from_node/
    graph_to_node) ищет узлы по принципу «оба id есть в этом графе», и общий
    id молча увёл бы километры в чужую область. Отдельная проверка нужна
    потому, что смены одной строки idPrefix недостаточно — id уже созданных
    узлов остаются со старой буквой (чинит scripts/repair_id_prefix.py);
  * idPrefix не занят служебными буквами: «E» начинает id рёбер, «X» —
    псевдоузлы внешних связей, и узел с таким id столкнулся бы с ними
    внутри собственного файла.

Код возврата: 0 — всё чисто, 1 — есть ошибки (для CI/pre-push hook).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))   # чтобы импортировался schema.py рядом с server.py

from schema import validate_graph  # noqa: E402


# Буквы, которыми начинаются НЕ узлы: id рёбер — «E», псевдоузлы внешних
# связей — «X». Узлу такой префикс отдавать нельзя (см. докстринг).
RESERVED_PREFIXES = {"E", "X"}


def collect_id_prefixes(all_files: list[Path]) -> dict[str, list[str]]:
    by_prefix: dict[str, list[str]] = {}
    for p in all_files:
        try:
            reg = json.loads(p.read_text(encoding="utf-8"))["meta"]["registry"]
        except Exception:  # noqa: BLE001 — свою ошибку файл получит через validate_graph
            continue
        by_prefix.setdefault(reg.get("idPrefix", ""), []).append(reg.get("slug", p.stem))
    return by_prefix


def collect_node_owners(all_files: list[Path]) -> dict[str, list[str]]:
    """{id узла: [области, где он встречается]} по ВСЕМ файлам сразу."""
    owners: dict[str, list[str]] = {}
    for p in all_files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            slug = data["meta"]["registry"].get("slug", p.stem)
        except Exception:  # noqa: BLE001
            continue
        for n in data.get("nodes", []) or []:
            nid = n.get("id") if isinstance(n, dict) else None
            if nid:
                owners.setdefault(nid, []).append(slug)
    return owners


def main() -> None:
    args = sys.argv[1:]
    regions_dir = HERE.parent / "regions"
    all_files = sorted(regions_dir.glob("*.json"))
    targets = [Path(a) for a in args] if args else all_files

    if not all_files:
        sys.exit("regions/ пуст или не найден")

    prefixes = collect_id_prefixes(all_files)
    collisions = {p: slugs for p, slugs in prefixes.items() if len(slugs) > 1}
    node_owners = collect_node_owners(all_files)

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
        slug = reg.get("slug", path.stem)
        if pfx in collisions:
            others = [s for s in collisions[pfx] if s != slug]
            errors.append(f"idPrefix {pfx!r} также занят: {', '.join(others)} "
                          f"— выберите другой (scripts/new_region.py делает это "
                          f"автоматически)")
        if pfx in RESERVED_PREFIXES:
            errors.append(f"idPrefix {pfx!r} зарезервирован: «E» — id рёбер, "
                          f"«X» — псевдоузлы внешних связей; смените буквы "
                          f"(scripts/repair_id_prefix.py {slug} <буква>)")

        # Один и тот же id узла в двух областях — та самая коллизия, из-за
        # которой server.py пропускает область целиком.
        shared: dict[str, list[str]] = {}
        for n in data.get("nodes", []) or []:
            nid = n.get("id") if isinstance(n, dict) else None
            others = [s for s in node_owners.get(nid, ()) if s != slug]
            if nid and others:
                shared[nid] = others
        if shared:
            sample = ", ".join(sorted(shared)[:5])
            with_slugs = sorted({s for v in shared.values() for s in v})
            errors.append(
                f"{len(shared)} id узлов встречаются также в области(ях) "
                f"{', '.join(with_slugs)} (например: {sample}) — перенумеруйте "
                f"узлы: scripts/repair_id_prefix.py {slug} <новая буква>")

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
