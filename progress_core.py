"""Привязка отчётов о работах к рёбрам графа ВОЛС.

Чистая логика без БД и веб-фреймворка (тестируется отдельно):
  * Graph            — граф из regions/<slug>.json: поиск узла по названию
                       (нечёткий, каз/рус), кратчайший маршрут между узлами;
  * compute_progress — строки БД (work_reports × report_metrics) →
                       {edgeId: {doneKm, reports}} + trace по каждому отчёту
                       (для debug-панели) + список непривязанных;
  * compute_smr      — свод СМР из «Отчет СОИ» (факт по СЁЛАМ, smr/<slug>.json)
                       → доля выполнения по рёбрам и статус по сёлам.

Правила привязки одного отчёта:
  0. Если строка отчёта несёт ПРИВЯЗКУ ОТ БОТА (graph_from_node/graph_to_node —
     id узлов графа, вычисленные ботом и подтверждённые работником в чате при
     сохранении отчёта) — используется она, фаззи-поиск не применяется вовсе.
  1. Иначе «куда» = settlement_name (или direction_to / имя объекта из
     справочника), «откуда» = direction_from. Оба резолвятся нечётким поиском
     по узлам графа (порог 60/100, бонус СНП и совпадению района, штраф за
     явно чужой район). ОГРАНИЧИТЕЛЬ: назначение со счётом ниже BIND_MIN не
     привязывается — отчёт уходит в unmatched (см. промах #76: «Дикан» из
     Жуалинского района ложился на «Қызылдикан → М 7» в Сарысуйском).
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

from matcher import districts_compatible, match_score, normalize_name

# Показатели, означающие километры магистрали. Порядок = приоритет:
# для прогресс-бара берётся ПЕРВЫЙ заполненный в отчёте.
KM_PRIORITY = ("vok_microcable", "pet_microtube", "trench", "trench_total", "signal_tape")
KM_METRICS = list(KM_PRIORITY)

PLANNED_TYPES = ("planned", "larger_capacity")
# Головные узлы сети — от них «течёт» интернет к сёлам. Тот же список, что у
# бота (app/services/netgraph.HEAD_KINDS) плюс netengine: он нужен здесь как
# точка отсчёта «выше/ниже по цепочке» при дележе рёбер между сёлами-соседями.
HEAD_KINDS = ("ats", "olt", "atn", "netengine")

MATCH_THRESHOLD = 60
# СТРОГИЙ ОГРАНИЧИТЕЛЬ ПРИВЯЗКИ: чтобы км легли на ребро, назначение должно
# быть найдено С УВЕРЕННОСТЬЮ не ниже этого счёта (после бонусов). Точное
# совпадение, префикс, пословное включение и опечатка в 1 букву дают ≥86;
# случайное вхождение внутри чужого названия («дикан» ⊂ «кызылдикан») — ≤69
# и через порог не проходит. Сомнительный отчёт уходит в unmatched (виден в
# debug-панели), а НЕ ложится молча на чужое ребро. Порог не действует, когда
# привязка (id узлов) пришла от бота — её подтвердил сам работник в чате.
BIND_MIN = 75
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
        # Район отчёта известен и у узла тоже: совпал — бонус, ЯВНО ЧУЖОЙ —
        # штраф (кандидат из другого района должен проигрывать местному даже
        # при чуть худшем совпадении названия: участок работ не прыгает через
        # районы, см. промах #76 — «Дикан» уезжал в Сарысуйский район).
        if dn and ndn:
            sc += 8 if districts_compatible(dn, ndn) else -15
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


def resolve_report_endpoints(graph: Graph, to_raw, from_raw, alt_from_raw,
                             district) -> tuple[dict | None, int, dict | None, int]:
    """Резолв обоих концов участка отчёта по названиям — ЕДИНАЯ точка правды.

    Используется в двух местах: здесь (compute_progress, фаззи-фолбэк для
    отчётов без привязки) и БОТОМ (app/services/graphbind.py импортирует этот
    модуль), который делает ту же привязку заранее — при подтверждении отчёта
    работником. Логика обязана совпадать, иначе бот показал бы работнику один
    участок, а вьюер нарисовал бы другой.

    Возвращает (to_node, to_sc, from_node, from_sc); from_node=None — «откуда»
    не найден или это на деле само назначение (см. правило 2 в докстринге)."""
    to_node, to_sc = graph.find_node_scored(to_raw, district)
    if to_node is None:
        return None, 0, None, 0
    from_node, from_sc = (graph.find_node_scored(from_raw, district,
                                                 near_id=to_node["id"])
                          if from_raw else (None, 0))
    # «Откуда» — на деле само назначение («Кунбатыс — М3» при объекте
    # «Күнбатыс 2»): имя без номера матчится на назначение не хуже, чем
    # на лучшего кандидата, — не даём фаззи-поиску увести его на тёзку.
    if from_node is not None and (
            from_node["id"] == to_node["id"]
            or graph.node_score(from_raw, district, to_node) >= from_sc):
        from_node, from_sc = None, 0
    # Настоящий второй конец тогда — alt_from (direction_to: муфта/АТС/село).
    if from_node is None and alt_from_raw:
        alt, alt_sc = graph.find_node_scored(alt_from_raw, district,
                                             near_id=to_node["id"])
        if alt is not None and alt["id"] != to_node["id"]:
            from_node, from_sc = alt, alt_sc
    return to_node, to_sc, from_node, from_sc


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
            # Привязка, вычисленная БОТОМ и подтверждённая работником в чате
            # (id узлов графа) — приоритетнее любого фаззи-поиска по названиям.
            "graph_from": r.get("graph_from_node"),
            "graph_to": r.get("graph_to_node"),
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

        # Привязка от бота (id узлов, подтверждены работником в чате) — если
        # есть и узлы живы в графе, фаззи-поиск по названиям не нужен вовсе.
        bound_to = graph.nodes.get(rep.get("graph_to") or "")
        if bound_to is not None:
            bound_from = graph.nodes.get(rep.get("graph_from") or "")
            to_node, to_sc = bound_to, 100
            from_node, from_sc = bound_from, (100 if bound_from else 0)
            entry["bind"] = "bot"
        else:
            to_node, to_sc, from_node, from_sc = resolve_report_endpoints(
                graph, rep["to"], rep["from"], rep.get("alt_from"),
                rep["district"])
            entry["bind"] = "fuzzy"
            if to_node is None:
                trace.append({**entry, "ok": False,
                              "reason": "назначение не найдено в графе"})
                unmatched.append({"from": rep["from"], "to": rep["to"], "km": km,
                                  "reason": "назначение не найдено в графе"})
                continue
            # ОГРАНИЧИТЕЛЬ (BIND_MIN): сомнительное назначение НЕ привязываем —
            # лучше честный unmatched в debug-панели, чем км на чужом ребре.
            if to_sc < BIND_MIN:
                reason = (f"назначение сомнительно (лучший кандидат "
                          f"«{to_node['name']}», счёт {to_sc}) — не привязано")
                trace.append({**entry, "ok": False, "reason": reason})
                unmatched.append({"from": rep["from"], "to": rep["to"], "km": km,
                                  "reason": reason})
                continue
            # Сомнительное «откуда» маршрут не строит: без него сработает
            # правило единственной плановой линии либо честный unmatched.
            if from_node is not None and from_sc < BIND_MIN:
                from_node = None
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


# ===========================================================================
# Свод СМР («Отчет СОИ», лист «СМР») — факт подрядчиков по СЁЛАМ
# ===========================================================================
# Свод даёт километры НА СЕЛО (план ВОЛС до НП, факт магистральной трубки и
# задувки ВОК), а не по сегментам чертежа: строка «с.Алгабас — план 10.9 км»
# покрывает и общий ствол ветки, по которому к селу идут ещё три соседа.
# Раскладывать эти километры по конкретным рёбрам как факт нельзя — получилось
# бы враньё с точностью до сегмента. Поэтому на карту переносится ДОЛЯ
# выполнения села: сколько процентов своего плана село прошло, столько же
# закрашивается его собственная ветка. Отсюда два ограничения, о которых
# должен знать любой, кто трогает этот код:
#
#   * доля > 100 % обрезается (факт трубки регулярно превышает план: свод
#     считает трассу до НП, а трубку кладут и по распредсети внутри села);
#   * ветка села — это НЕ «все рёбра до головного узла», а только те, что
#     ближе к нему, чем к любому другому селу (см. attribute_edges_to_snp).
#     Иначе общий ствол закрасился бы столько раз, сколько сёл на нём висит.


def _multi_source_dist(graph: Graph, sources, edge_types=None) -> dict[str, float]:
    """Дейкстра от МНОЖЕСТВА источников сразу: {node_id: расстояние в км до
    ближайшего источника}. edge_types=None — идём по всем рёбрам."""
    dist = {s: 0.0 for s in sources if s in graph.nodes}
    pq = [(0.0, s) for s in dist]
    heapq.heapify(pq)
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, float("inf")) + _EPS:
            continue
        for e in graph.adj.get(u, ()):
            if edge_types is not None and e.get("type") not in edge_types:
                continue
            v = e["to"] if e["from"] == u else e["from"]
            nd = d + (e.get("lengthKm") or 0.05)
            if nd < dist.get(v, float("inf")) - _EPS:
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


def attribute_edges_to_snp(graph: Graph) -> dict[str, str]:
    """{edgeId: id СНП-узла, которому принадлежит этот плановый сегмент}.

    Делёж по принципу «ближайшего села» (по плановым рёбрам): каждый сегмент
    достаётся тому селу, к которому он ближе. Это разбивает плановую сеть на
    непересекающиеся ветки — сумма веток равна всей сети, и ни один километр
    не учитывается дважды.

    Спорный случай — сегмент прямо между двумя сёлами (оба на нуле). Он
    достаётся тому, которое ДАЛЬШЕ от головного узла: чтобы подключить дальнее
    село, сначала строят к ближнему, поэтому участок между ними относится к
    дальнему — то же правило, по которому бот определяет направление работ
    (app/services/netgraph.py)."""
    snp_ids = [n["id"] for n in graph.nodes.values() if n.get("kind") == "snp"]
    if not snp_ids:
        return {}
    heads = [n["id"] for n in graph.nodes.values() if n.get("kind") in HEAD_KINDS]

    # Ближайшее село для каждого узла — обход от всех сёл сразу, с запоминанием,
    # от какого именно источника пришли.
    owner: dict[str, str] = {s: s for s in snp_ids}
    dist: dict[str, float] = {s: 0.0 for s in snp_ids}
    pq = [(0.0, s, s) for s in snp_ids]
    heapq.heapify(pq)
    while pq:
        d, u, src = heapq.heappop(pq)
        if d > dist.get(u, float("inf")) + _EPS:
            continue
        for e in graph.adj.get(u, ()):
            if e.get("type") not in PLANNED_TYPES:
                continue
            v = e["to"] if e["from"] == u else e["from"]
            nd = d + (e.get("lengthKm") or 0.05)
            if nd < dist.get(v, float("inf")) - _EPS:
                dist[v], owner[v] = nd, src
                heapq.heappush(pq, (nd, v, src))

    head_dist = _multi_source_dist(graph, heads) if heads else {}
    inf = float("inf")
    out: dict[str, str] = {}
    for e in graph.edges:
        if e.get("type") not in PLANNED_TYPES:
            continue
        a, b = e["from"], e["to"]
        oa, ob = owner.get(a), owner.get(b)
        if oa is None and ob is None:
            continue
        if oa is None or ob is None:
            out[e["id"]] = oa or ob
            continue
        if oa == ob:
            out[e["id"]] = oa
            continue
        da, db = dist.get(a, inf), dist.get(b, inf)
        if abs(da - db) > _EPS:
            out[e["id"]] = oa if da < db else ob
        else:  # ничья — берём село ниже по цепочке (дальше от головного узла)
            out[e["id"]] = oa if head_dist.get(oa, -1) >= head_dist.get(ob, -1) else ob
    return out


def _snp_branches(graph: Graph) -> dict[str, list[dict]]:
    """{snpId: рёбра его ветки, упорядоченные СВЕРХУ ВНИЗ (от головного узла
    к селу)} — порядок нужен, чтобы закрашивать ветку так, как её физически
    строят: от уже готовой сети в сторону села."""
    heads = [n["id"] for n in graph.nodes.values() if n.get("kind") in HEAD_KINDS]
    head_dist = _multi_source_dist(graph, heads) if heads else {}
    edge_by_id = {e["id"]: e for e in graph.edges}
    branches: dict[str, list[dict]] = {}
    for eid, snp_id in attribute_edges_to_snp(graph).items():
        branches.setdefault(snp_id, []).append(edge_by_id[eid])
    inf = float("inf")
    for edges in branches.values():
        edges.sort(key=lambda e: min(head_dist.get(e["from"], inf),
                                     head_dist.get(e["to"], inf)))
    return branches


def _fill_branch(branch: list[dict], frac: float, head_dist: dict[str, float]
                 ) -> dict[str, tuple[float, str]]:
    """Закрасить долю frac ветки: рёбра заполняются последовательно сверху
    вниз, а не все сразу на frac — стройка идёт от готовой сети к селу, и
    «половина ветки» выглядит как пройденная первая половина, а не как
    полупрозрачная вся ветка целиком.

    Возвращает {edgeId: (доля закраски 0..1, id узла, ОТ которого красим)}."""
    inf = float("inf")

    def fill_from(e: dict) -> str:
        """Конец ребра, что ближе к головному узлу, — от него и красим."""
        return (e["from"] if head_dist.get(e["from"], inf) <= head_dist.get(e["to"], inf)
                else e["to"])

    lengths = [e.get("lengthKm") or 0.0 for e in branch]
    total = sum(lengths)
    if total <= _EPS:  # длин сегментов на чертеже нет — красим ветку поровну
        return {e["id"]: (frac, fill_from(e)) for e in branch}

    remaining = total * frac
    out: dict[str, tuple[float, str]] = {}
    for e, length in zip(branch, lengths):
        if remaining <= _EPS:
            break
        if length <= _EPS:  # сегмент без длины не «съедает» остаток
            out[e["id"]] = (1.0, fill_from(e))
            continue
        take = min(remaining, length)
        remaining -= take
        out[e["id"]] = (round(take / length, 4), fill_from(e))
    return out


def compute_smr(graph: Graph, smr: dict) -> dict:
    """smr — содержимое smr/<slug>.json (см. scripts/import_smr.py).

    Возвращает payload для /api/smr:
      * edges  {edgeId: {frac, fillFrom, snp, metric}} — доля выполнения ветки;
      * nodes  {nodeId: {planKm, tubeKm, fiberKm, done, frac, metric}} — факт
               по селу как он есть в своде, без всякой интерпретации;
      * totals — суммы по своду и по тому, что удалось положить на граф."""
    settlements = smr.get("settlements") or []
    heads = [n["id"] for n in graph.nodes.values() if n.get("kind") in HEAD_KINDS]
    head_dist = _multi_source_dist(graph, heads) if heads else {}
    branches = _snp_branches(graph)

    out_nodes: dict[str, dict] = {}
    out_edges: dict[str, dict] = {}
    no_branch: list[dict] = []
    plan_km = tube_km = fiber_km = 0.0
    snp_done = 0
    painted_km = 0.0

    for s in settlements:
        nid = s.get("nodeId")
        if not nid or nid not in graph.nodes:
            continue
        plan = s.get("planKm") or 0.0
        tube = s.get("tubeKm") or 0.0
        fiber = s.get("fiberKm") or 0.0
        done = bool(s.get("snpDone"))
        plan_km += plan
        tube_km += tube
        fiber_km += fiber
        snp_done += 1 if done else 0

        # Приоритет тот же, что у отчётов бота: задувка ВОК главнее трубки —
        # трубка без волокна связь ещё не даёт.
        metric, fact = ("fiber", fiber) if fiber > 0 else ("tube", tube)
        frac = 1.0 if done else (min(1.0, fact / plan) if plan > 0 else (1.0 if fact > 0 else 0.0))

        node = graph.nodes[nid]
        out_nodes[nid] = {
            "name": node.get("name"), "smrName": s.get("name"),
            "district": s.get("district"), "contractor": s.get("contractor"),
            "planKm": round(plan, 3) or None, "tubeKm": round(tube, 3) or None,
            "fiberKm": round(fiber, 3) or None,
            "guboDone": s.get("guboDone") or None, "b2cDone": s.get("b2cDone") or None,
            "done": done, "frac": round(frac, 4), "metric": metric,
        }

        branch = branches.get(nid)
        if not branch:
            if frac > 0:
                no_branch.append({"nodeId": nid, "name": node.get("name"),
                                  "reason": "у села нет плановых линий в графе"})
            continue
        if frac <= 0:
            continue
        for eid, (edge_frac, fill_from) in _fill_branch(branch, frac, head_dist).items():
            out_edges[eid] = {"frac": edge_frac, "fillFrom": fill_from,
                              "snp": nid, "metric": metric}
        painted_km += sum((e.get("lengthKm") or 0.0) for e in branch) * frac

    meta_totals = (smr.get("meta") or {}).get("totals") or {}
    return {
        "edges": out_edges,
        "nodes": out_nodes,
        "noBranch": no_branch,
        "asOf": (smr.get("meta") or {}).get("asOf"),
        "totals": {
            # По своду целиком (включая сёла, которых нет в графе).
            "smrPlanKm": meta_totals.get("planKm"),
            "smrTubeKm": meta_totals.get("tubeKm"),
            "smrFiberKm": meta_totals.get("fiberKm"),
            "smrSnpDone": meta_totals.get("snpDone"),
            # По сёлам, привязанным к узлам этого графа.
            "planKm": round(plan_km, 1),
            "tubeKm": round(tube_km, 1),
            "fiberKm": round(fiber_km, 1),
            "snpDone": snp_done,
            "snpMatched": len(out_nodes),
            "snpUnmatched": len(smr.get("unmatched") or []),
            # Сколько километров ГРАФА закрашено этой долей — не путать с
            # километрами свода: это длина веток, а не факт подрядчика.
            "graphPaintedKm": round(painted_km, 1),
        },
    }
