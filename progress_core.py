"""Привязка отчётов о работах к рёбрам графа ВОЛС.

Чистая логика без БД и веб-фреймворка (тестируется отдельно):
  * Graph            — граф из zhambyl-graph.json: поиск узла по названию
                       (нечёткий, каз/рус), кратчайший маршрут между узлами;
  * compute_progress — строки БД (work_reports × report_metrics) →
                       {edgeId: {doneKm, reports}} + trace по каждому отчёту
                       (для debug-панели) + список непривязанных.

Правила привязки одного отчёта:
  1. «Куда» = settlement_name (или direction_to / имя объекта из справочника),
     «откуда» = direction_from. Оба резолвятся нечётким поиском по узлам графа
     (порог 60/100, бонус СНП и совпадению района).
  2. Если «откуда» — на деле само село-назначение (работник пишет участок как
     «село — муфта»: «Кунбатыс — М3» при объекте «Күнбатыс 2», без номера имя
     матчится на назначение не хуже, чем на любого тёзку) либо «откуда» не
     найден вовсе — вторым концом участка берётся direction_to (муфта/АТС/
     соседнее село). Иначе фаззи-поиск молча уводил бы такой конец на тёзку
     (Күнбатыс 1) и км ложились на чужое ребро.
  3. Км работ = первый заполненный показатель по приоритету
     задувка ВОК → микротрубка ПЭТ → траншея (KM_PRIORITY).
  4. Если «откуда» найден — кратчайший маршрут по графу, км укладываются
     по рёбрам последовательно от «откуда» (как физически идёт стройка).
  5. Если «откуда» нет, но у села-назначения ровно одна плановая линия —
     км ложатся на неё (заполнение к селу).
  6. Иначе отчёт попадает в unmatched — виден в ответе API для разбора.

ВАЖНО про рёбра без lengthKm: часть сегментов на чертеже не имеет плановой
длины (lengthKm=null — на 2026-07 таких 9 из 516, например прямая линия
Ақтоған—Жаңаталап). Км с отчёта на таком ребре НЕ обрезаются длиной (её нет),
а полностью засчитываются как doneKm; на фронте это ребро просто рисуется
«выполнено» без дроби «X/Y км», т.к. знаменателя нет.
"""
import heapq
import json
from pathlib import Path

from matcher import match_score, normalize_name

# Показатели, означающие километры магистрали. Порядок = приоритет:
# для прогресс-бара берётся ПЕРВЫЙ заполненный в отчёте.
KM_PRIORITY = ("vok_microcable", "pet_microtube", "trench", "trench_total", "signal_tape")
KM_METRICS = list(KM_PRIORITY)

MATCH_THRESHOLD = 60
_EPS = 1e-9
TRACE_LIMIT = 300  # сколько последних отчётов держим в trace для debug-панели


class Graph:
    def __init__(self, path: str | Path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.meta = data.get("meta", {})
        self.nodes = {n["id"]: n for n in data["nodes"]}
        self.edges = [e for e in data["edges"]
                      if e["from"] in self.nodes and e["to"] in self.nodes]
        self.adj: dict[str, list[dict]] = {}
        for e in self.edges:
            self.adj.setdefault(e["from"], []).append(e)
            self.adj.setdefault(e["to"], []).append(e)
        # Индекс поиска: только именованные узлы, с предвычисленной нормализацией.
        self.named = []
        for n in data["nodes"]:
            nn = normalize_name(n.get("name"))
            if nn:
                self.named.append((nn, normalize_name(n.get("district")), n))
        self._hops_cache: dict[str, dict[str, int]] = {}

    @staticmethod
    def _score(qn: str, dn: str, nn: str, ndn: str, n: dict) -> int:
        sc = match_score(qn, nn)
        if sc <= 0:
            return 0
        if n.get("kind") == "snp":
            sc += 6
        if dn and ndn and (dn in ndn or ndn in dn):
            sc += 8
        return sc

    def find_node(self, name: str | None, district: str | None = None,
                  min_score: int = MATCH_THRESHOLD) -> dict | None:
        node, _ = self.find_node_scored(name, district, min_score)
        return node

    def find_node_scored(self, name: str | None, district: str | None = None,
                         min_score: int = MATCH_THRESHOLD,
                         near_id: str | None = None) -> tuple[dict | None, int]:
        """(узел, счёт) лучшего совпадения; (None, 0), если ниже порога.

        near_id — якорь для тёзок: муфты нумеруются В ПРЕДЕЛАХ ВЕТКИ («М 3»
        в графе встречается многократно, даже внутри одного района), поэтому
        из кандидатов С РАВНЫМ счётом берётся ближайший ПО ГРАФУ к якорю
        (селу-назначению отчёта) — участок связывает близкие точки."""
        qn = normalize_name(name)
        if len(qn) < 2:
            return None, 0
        dn = normalize_name(district)
        best_sc, cands = 0, []
        for nn, ndn, n in self.named:
            sc = self._score(qn, dn, nn, ndn, n)
            if sc > best_sc:
                best_sc, cands = sc, [n]
            elif sc == best_sc and sc > 0:
                cands.append(n)
        if best_sc < min_score:
            return None, 0
        if len(cands) > 1 and near_id:
            hops = self._hops_from(near_id)
            cands.sort(key=lambda n: hops.get(n["id"], float("inf")))
        return cands[0], best_sc

    def _hops_from(self, node_id: str) -> dict[str, int]:
        """Число хопов от узла до всех достижимых (BFS, кэш на граф)."""
        cached = self._hops_cache.get(node_id)
        if cached is not None:
            return cached
        dist = {node_id: 0}
        queue = [node_id]
        for cur in queue:  # список растёт по ходу обхода — это и есть очередь
            for e in self.adj.get(cur, ()):
                nxt = e["to"] if e["from"] == cur else e["from"]
                if nxt not in dist:
                    dist[nxt] = dist[cur] + 1
                    queue.append(nxt)
        self._hops_cache[node_id] = dist
        return dist

    def node_score(self, name: str | None, district: str | None, node: dict) -> int:
        """Счёт КОНКРЕТНОГО узла для запроса — та же шкала, что у find_node
        (для сравнения «а не назначение ли это на самом деле», см.
        compute_progress)."""
        qn = normalize_name(name)
        if len(qn) < 2 or node is None:
            return 0
        return self._score(qn, normalize_name(district),
                           normalize_name(node.get("name")),
                           normalize_name(node.get("district")), node)

    def shortest_path(self, a_id: str, b_id: str) -> list[dict] | None:
        """Дейкстра по длинам сегментов; рёбра без длины почти бесплатны для
        маршрутизации (см. fill_along — по факту км они всё равно получают).
        Возвращает рёбра маршрута в порядке следования от a_id."""
        if a_id == b_id:
            return []
        dist = {a_id: 0.0}
        prev_edge: dict[str, dict] = {}
        pq = [(0.0, a_id)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == b_id:
                break
            if d > dist.get(u, float("inf")) + _EPS:
                continue
            for e in self.adj.get(u, ()):
                v = e["to"] if e["from"] == u else e["from"]
                nd = d + (e.get("lengthKm") or 0.05)
                if nd < dist.get(v, float("inf")) - _EPS:
                    dist[v] = nd
                    prev_edge[v] = e
                    heapq.heappush(pq, (nd, v))
        if b_id not in prev_edge:
            return None
        path, cur = [], b_id
        while cur != a_id:
            e = prev_edge[cur]
            path.append(e)
            cur = e["from"] if e["to"] == cur else e["to"]
        path.reverse()
        return path


def fill_along(path: list[dict], start_id: str, km: float) -> tuple[dict[str, dict], float]:
    """Разложить km по рёбрам маршрута последовательно, начиная со start_id.

    Рёбра с известной длиной — обычная нарезка (min(remaining, length)).
    Рёбра БЕЗ длины (lengthKm=null, чертёж не даёт числа) — забирают весь
    остаток на себя, а не блокируют раскладку: км не должны молча теряться
    только из-за дырки в исходных данных чертежа.

    Возвращает (доли по рёбрам {edgeId: {km, fillFrom}}, неразмещённый остаток км —
    остаток > 0 означает, что факт превысил суммарную ИЗВЕСТНУЮ длину маршрута)."""
    result: dict[str, dict] = {}
    cur, remaining = start_id, km
    for e in path:
        nxt = e["to"] if e["from"] == cur else e["from"]
        length = e.get("lengthKm")
        if remaining > _EPS:
            add = min(remaining, length) if length else remaining
            slot = result.setdefault(e["id"], {"km": 0.0, "fillFrom": cur})
            slot["km"] += add
            remaining -= add
        cur = nxt
        if remaining <= _EPS:
            break
    return result, max(0.0, remaining)


def _iso(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)


def compute_progress(graph: Graph, rows: list[dict]) -> dict:
    """rows — строки SQL (см. server.py): по одной на (отчёт × показатель).
    Возвращает payload для /api/progress, включая edges/unmatched/totals
    и trace — по одной записи на КАЖДЫЙ отчёт (для debug-панели фронта):
    что распознали, на какое ребро легло или почему нет."""
    # 1. Сгруппировать строки по отчёту.
    reports: dict[int, dict] = {}
    for r in rows:
        to_raw = (r.get("settlement_name") or r.get("direction_to")
                  or r.get("object_name"))
        rep = reports.setdefault(r["report_id"], {
            "report_id": r["report_id"],
            "from": r.get("direction_from"),
            "to": to_raw,
            # Запасной второй конец участка: direction_to, если «куда» взято
            # не из него (см. правило 2 в докстринге модуля).
            "alt_from": r.get("direction_to")
                        if r.get("direction_to") and r.get("direction_to") != to_raw
                        else None,
            "district": r.get("district_name") or r.get("object_district"),
            "reported_at": r.get("reported_at"),
            "submitted_at": r.get("submitted_at"),
            "metrics": {},
        })
        if r.get("value_numeric") is not None:
            rep["metrics"][r["code"]] = float(r["value_numeric"])

    # 2. Резолв концов (по КАЖДОМУ отчёту — это и есть trace), группировка в пары
    #    для последующей маршрутизации (несколько отчётов на одну пару складываются).
    pairs: dict[tuple, dict] = {}
    trace: list[dict] = []
    unmatched: list[dict] = []
    for rep in sorted(reports.values(), key=lambda r: r["report_id"], reverse=True):
        entry = {
            "reportId": rep["report_id"], "from": rep["from"], "to": rep["to"],
            "district": rep["district"], "reportedAt": _iso(rep["reported_at"]),
            "submittedAt": _iso(rep["submitted_at"]),
        }
        km_code = next((c for c in KM_PRIORITY if rep["metrics"].get(c, 0) > 0), None)
        if km_code is None:
            trace.append({**entry, "ok": False, "reason": "нет километровых показателей"})
            continue
        km = rep["metrics"][km_code]
        entry.update(km=km, metric=km_code)

        to_node = graph.find_node(rep["to"], rep["district"])
        if to_node is None:
            trace.append({**entry, "ok": False, "reason": "назначение не найдено в графе"})
            unmatched.append({"from": rep["from"], "to": rep["to"], "km": km,
                              "reason": "назначение не найдено в графе"})
            continue
        from_node, from_sc = (graph.find_node_scored(rep["from"], rep["district"],
                                                     near_id=to_node["id"])
                              if rep["from"] else (None, 0))
        # «Откуда» — на деле само назначение («Кунбатыс — М3» при объекте
        # «Күнбатыс 2»): имя без номера матчится на назначение не хуже, чем
        # на лучшего кандидата, — не даём фаззи-поиску увести его на тёзку.
        if from_node is not None and (
                from_node["id"] == to_node["id"]
                or graph.node_score(rep["from"], rep["district"], to_node) >= from_sc):
            from_node = None
        # Настоящий второй конец тогда — direction_to (муфта/АТС/село).
        if from_node is None and rep.get("alt_from"):
            alt, _ = graph.find_node_scored(rep["alt_from"], rep["district"],
                                            near_id=to_node["id"])
            if alt is not None and alt["id"] != to_node["id"]:
                from_node = alt
        entry.update(toNode=to_node["name"], toNodeId=to_node["id"],
                    fromNode=from_node["name"] if from_node else None,
                    fromNodeId=from_node["id"] if from_node else None)

        key = (from_node["id"] if from_node else None, to_node["id"])
        p = pairs.setdefault(key, {"km": 0.0, "n": 0, "from": rep["from"], "to": rep["to"],
                                   "entries": []})
        p["km"] += km
        p["n"] += 1
        p["entries"].append(entry)
        trace.append(entry)

    # 3. Маршрут по графу и раскладка км по рёбрам; результат дописывается
    #    обратно в trace-записи (edges или reason) через p["entries"].
    edges_km: dict[str, float] = {}
    edges_n: dict[str, int] = {}
    edges_from: dict[str, str] = {}  # с какой стороны идёт заливка
    for (fid, tid), p in pairs.items():
        if fid is not None:
            path = graph.shortest_path(fid, tid)
            if not path:
                reason = "нет маршрута между точками в графе"
                unmatched.append({"from": p["from"], "to": p["to"],
                                  "km": round(p["km"], 2), "reason": reason})
                for e in p["entries"]:
                    e.update(ok=False, reason=reason)
                continue
            start = fid
        else:
            planned = [e for e in graph.adj.get(tid, ())
                       if e.get("type") in ("planned", "larger_capacity")]
            if len(planned) != 1:
                reason = "нет «откуда», участок неоднозначен"
                unmatched.append({"from": p["from"], "to": p["to"],
                                  "km": round(p["km"], 2), "reason": reason})
                for e in p["entries"]:
                    e.update(ok=False, reason=reason)
                continue
            e0 = planned[0]
            path = [e0]
            start = e0["to"] if e0["from"] == tid else e0["from"]  # заполняем К селу

        filled, leftover = fill_along(path, start, p["km"])
        edge_ids = list(filled.keys())
        for eid, slot in filled.items():
            edges_km[eid] = edges_km.get(eid, 0) + slot["km"]
            edges_n[eid] = edges_n.get(eid, 0) + p["n"]
            edges_from.setdefault(eid, slot["fillFrom"])
        for e in p["entries"]:
            e.update(ok=True, edges=edge_ids)
        if leftover > 0.05:  # факт превышает суммарную ИЗВЕСТНУЮ длину маршрута
            unmatched.append({"from": p["from"], "to": p["to"],
                              "km": round(leftover, 2),
                              "reason": "остаток сверх плановой длины участка"})

    # 4. Payload: doneKm обрезаем плановой длиной сегмента (если она известна).
    edge_by_id = {e["id"]: e for e in graph.edges}
    out_edges = {}
    total_done = 0.0
    for eid, km in edges_km.items():
        length = edge_by_id[eid].get("lengthKm")
        done = round(min(km, length) if length else km, 2)
        out_edges[eid] = {"doneKm": done, "reports": edges_n[eid],
                          "fillFrom": edges_from.get(eid)}
        total_done += done
    return {
        "edges": out_edges,
        "unmatched": unmatched,
        "trace": trace[:TRACE_LIMIT],
        "totals": {
            "doneKm": round(total_done, 1),
            "plannedKm": graph.meta.get("counts", {}).get("plannedKm"),
            "reportsMatched": sum(edges_n.values()),
            "reportsUnmatched": len(unmatched),
            "reportsTotal": len(reports),
        },
    }
