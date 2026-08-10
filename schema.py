"""Схема графа ВОЛС — общие константы и проверка структуры файла.

Один источник правды для допустимых kind/subtype/type: server.py импортирует
их для валидации правок с фронта, scripts/validate_region.py — тем же кодом
проверяет файл ПЕРЕД коммитом/PR, чтобы расхождение узла/сервера/скрипта не
пропустило в regions/ то, что фронт потом не сможет отрисовать.
"""
from __future__ import annotations

NODE_KINDS = ("snp", "ats", "olt", "atn", "netengine", "mufta")
SNP_SUBTYPES = ("fiber", "wifi", "starlink", "outside")
EQUIP_SUBTYPES = ("existing", "planned", "outside")
EDGE_TYPES = ("existing", "planned", "outside_project", "no_free_fibers", "larger_capacity")

NODE_FIELDS = ("name", "connectionCount", "x", "y")  # редактируемые с фронта
EDGE_FIELDS = ("lengthKm",)


def validate_graph(data: dict, *, file_slug: str | None = None) -> list[str]:
    """Список ошибок (пусто = файл корректен). Не бросает исключений —
    рассчитан и на CLI (scripts/validate_region.py), и на месте, где падать
    нельзя (server.py при обнаружении графов — один битый файл не должен
    ронять остальные, см. discover_graphs())."""
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["верхний уровень файла должен быть объектом"]

    meta = data.get("meta")
    if not isinstance(meta, dict):
        errors.append("нет meta")
    else:
        reg = meta.get("registry")
        if not isinstance(reg, dict):
            errors.append("нет meta.registry {slug, title, idPrefix}")
        else:
            for f in ("slug", "title", "idPrefix"):
                if not reg.get(f):
                    errors.append(f"meta.registry.{f} пусто или отсутствует")
            if file_slug and reg.get("slug") and reg["slug"] != file_slug:
                errors.append(
                    f"meta.registry.slug={reg['slug']!r} не совпадает с именем "
                    f"файла regions/{file_slug}.json")

    nodes = data.get("nodes")
    edges = data.get("edges")
    if not isinstance(nodes, list):
        errors.append("нет nodes (должен быть список)")
        nodes = []
    if not isinstance(edges, list):
        errors.append("нет edges (должен быть список)")
        edges = []

    seen_ids: set[str] = set()
    for i, n in enumerate(nodes):
        where = f"nodes[{i}]"
        if not isinstance(n, dict):
            errors.append(f"{where}: не объект")
            continue
        nid = n.get("id")
        if not nid:
            errors.append(f"{where}: нет id")
        elif nid in seen_ids:
            errors.append(f"{where}: дублирующийся id {nid!r}")
        else:
            seen_ids.add(nid)
        kind = n.get("kind")
        if kind not in NODE_KINDS:
            errors.append(f"{where} ({nid}): kind={kind!r} не из {NODE_KINDS}")
        sub = n.get("subtype")
        allowed_sub = SNP_SUBTYPES if kind == "snp" else EQUIP_SUBTYPES
        if kind in NODE_KINDS and sub not in allowed_sub:
            errors.append(f"{where} ({nid}): subtype={sub!r} не из {allowed_sub} "
                          f"(kind={kind})")
        for f in ("x", "y"):
            if not isinstance(n.get(f), (int, float)):
                errors.append(f"{where} ({nid}): {f} должен быть числом")

    for i, e in enumerate(edges):
        where = f"edges[{i}]"
        if not isinstance(e, dict):
            errors.append(f"{where}: не объект")
            continue
        eid = e.get("id")
        if not eid:
            errors.append(f"{where}: нет id")
        elif eid in seen_ids:
            errors.append(f"{where}: id {eid!r} совпадает с id узла")
        for f in ("from", "to"):
            if e.get(f) not in seen_ids:
                errors.append(f"{where} ({eid}): {f}={e.get(f)!r} — нет такого узла")
        if e.get("type") not in EDGE_TYPES:
            errors.append(f"{where} ({eid}): type={e.get('type')!r} не из {EDGE_TYPES}")
        km = e.get("lengthKm")
        if km is not None and (not isinstance(km, (int, float)) or km < 0):
            errors.append(f"{where} ({eid}): lengthKm должен быть числом ≥ 0 или null")

    for i, el in enumerate(data.get("externalLinks", []) or []):
        where = f"externalLinks[{i}]"
        if el.get("from") not in seen_ids:
            errors.append(f"{where}: from={el.get('from')!r} — нет такого узла")
        if not isinstance(el.get("externalEndpoint"), dict):
            errors.append(f"{where}: нет externalEndpoint")

    return errors
