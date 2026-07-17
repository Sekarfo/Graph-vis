"""HTTP-сервер вьюера: статика graph-vis + /api/progress из PostgreSQL
+ /api/graph/edit — редактирование графа с фронта (названия, плановые км)
с автосохранением в zhambyl-graph.json.

Деплой — где угодно, откуда доступен Postgres на AWS (например, на той же
машине, где БД); серверу бота внешние порты НЕ нужны — он сюда не ходит.
Без БД сервер тоже стартует: вьюер и редактирование графа работают,
а /api/progress отвечает 503 (прогресс появится, когда БД доступна).

    export DATABASE_URL=postgresql://user:pass@host:5432/db   # или ../.env
    pip install -r requirements.txt
    uvicorn server:app --host 0.0.0.0 --port 8080

Фронт (graph-app.js) поллит GET /api/progress раз в 30 сек; ответ кэшируется
на CACHE_TTL сек, а пересчитывается только если в БД появились новые строки
(дешёвая проверка по max(id)/count) — нагрузка на Postgres копеечная.
"""
import asyncio
import json
import logging
import os
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from progress_core import KM_METRICS, Graph, compute_progress

log = logging.getLogger("graph-viewer")

BASE = Path(__file__).parent
GRAPH_PATH = BASE / "zhambyl-graph.json"
DATA_JS_PATH = BASE / "graph-data.js"
CACHE_TTL = 10  # сек: чаще этого в БД не ходим, даже если фронтов много

SQL_ETAG = """
SELECT (SELECT COALESCE(max(id), 0) FROM work_reports)   AS wr_max,
       (SELECT COALESCE(max(id), 0) FROM report_metrics) AS rm_max,
       (SELECT count(*)             FROM report_metrics) AS rm_cnt
"""

SQL_ROWS = """
SELECT wr.id AS report_id,
       wr.direction_from, wr.direction_to, wr.settlement_name, wr.district_name,
       wr.reported_at, wr.submitted_at, wr.status,
       o.name  AS object_name,
       d.name  AS object_district,
       mt.code, rm.value_numeric
FROM work_reports wr
JOIN report_metrics rm ON rm.report_id = wr.id
JOIN metric_types  mt ON mt.id = rm.metric_type_id
LEFT JOIN objects   o ON o.id = wr.object_id
LEFT JOIN districts d ON d.id = o.district_id
WHERE wr.status <> 'rejected'
  AND rm.value_numeric IS NOT NULL
  AND mt.code = ANY($1::text[])
"""


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return url
    # fallback: .env в корне репозитория (тот же, что у бота)
    for env_path in (BASE.parent / ".env", BASE / ".env"):
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("DATABASE_URL не задан (env или .env)")


graph = Graph(GRAPH_PATH)
pool: asyncpg.Pool | None = None
_cache = {"t": 0.0, "etag": None, "payload": None}
_edit_lock = asyncio.Lock()  # правки графа сериализуем: файл один на всех


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    try:
        pool = await asyncpg.create_pool(dsn=_database_url(), min_size=1, max_size=4)
    except Exception as e:  # noqa: BLE001 — без БД тоже работаем (вьюер + правка)
        log.warning("БД недоступна (%s): вьюер и редактирование графа работают, "
                    "/api/progress — нет", e)
        pool = None
    yield
    if pool is not None:
        await pool.close()


app = FastAPI(title="auyl-bot graph viewer", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"],
)

# Защита правки для ссылок наружу (тоннель/публичный деплой): на /api/graph/*
# иначе нет вообще никакой авторизации, а ссылка может быть у кого угодно.
#   READ_ONLY=1          — правка отключена совсем (кнопка «✏️ Правка» во
#                           фронте останется видна, но сохранение — всегда 403).
#   EDIT_PASSWORD=<пароль> — правка разрешена только тем, кто вошёл паролем:
#                           POST /api/graph/login {"password"} выдаёт разовый
#                           токен сессии, дальше он идёт заголовком
#                           X-Edit-Token на каждую правку (см. apiPost/login
#                           в graph-app.js — фронт спрашивает пароль и логинится
#                           автоматически при первом же 401 от /api/graph/*).
# Ни один из флагов не задан — правка открыта всем (локальный/доверенный запуск).
READ_ONLY = os.environ.get("READ_ONLY", "").lower() in ("1", "true", "yes")
EDIT_PASSWORD = os.environ.get("EDIT_PASSWORD", "")
_EDIT_PATHS = {"/api/graph/edit", "/api/graph/add-node",
               "/api/graph/add-edge", "/api/graph/delete"}

# Сессии правки: token -> время выдачи. Только в памяти — рестарт сервера
# разлогинивает всех, это ожидаемо для лёгкой защиты внутреннего инструмента.
_sessions: dict[str, float] = {}
_SESSION_TTL = 12 * 3600  # 12 часов

# Простая защита /api/graph/login от перебора пароля (он короткий, 8 цифр):
# N неудачных попыток подряд с одного IP — временная блокировка.
_LOGIN_MAX_FAILS = 5
_LOGIN_LOCKOUT_SEC = 60
_login_fail_count: dict[str, int] = defaultdict(int)
_login_lockout_until: dict[str, float] = {}


def _valid_session(token: str) -> bool:
    if not token:
        return False
    issued = _sessions.get(token)
    if issued is None:
        return False
    if time.time() - issued > _SESSION_TTL:
        _sessions.pop(token, None)
        return False
    return True


@app.middleware("http")
async def _read_only_guard(request, call_next):
    if request.url.path in _EDIT_PATHS:
        from fastapi.responses import JSONResponse
        if READ_ONLY:
            return JSONResponse(
                {"ok": False, "detail": "Режим только просмотра: редактирование графа отключено."},
                status_code=403,
            )
        if EDIT_PASSWORD and not _valid_session(request.headers.get("X-Edit-Token", "")):
            return JSONResponse(
                {"ok": False, "detail": "Нужен вход в режим правки (пароль)."},
                status_code=401,
            )
    return await call_next(request)


class LoginRequest(BaseModel):
    password: str


@app.post("/api/graph/login")
async def graph_login(req: LoginRequest, request: Request):
    """Пароль → разовый токен сессии правки (см. _sessions/_valid_session).
    Без EDIT_PASSWORD в окружении вход всегда отклоняется — правка тогда
    либо открыта всем (см. _read_only_guard), либо выключена (READ_ONLY)."""
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    until = _login_lockout_until.get(ip, 0.0)
    if now < until:
        raise HTTPException(429, f"слишком много попыток, попробуйте через {int(until - now)} сек")

    if not EDIT_PASSWORD or req.password != EDIT_PASSWORD:
        _login_fail_count[ip] += 1
        if _login_fail_count[ip] >= _LOGIN_MAX_FAILS:
            _login_lockout_until[ip] = now + _LOGIN_LOCKOUT_SEC
            _login_fail_count[ip] = 0
        raise HTTPException(401, "неверный пароль")

    _login_fail_count[ip] = 0
    token = secrets.token_urlsafe(24)
    _sessions[token] = now
    log.info("graph-vis: выдан токен правки (ip=%s)", ip)
    return {"ok": True, "token": token}


@app.get("/api/health")
async def health():
    if pool is None:
        raise HTTPException(503, "db: подключение не настроено (DATABASE_URL)")
    try:
        async with pool.acquire() as con:
            await con.fetchval("SELECT 1")
        return {"ok": True}
    except Exception as e:  # noqa: BLE001 — health должен отвечать, а не падать
        raise HTTPException(503, f"db: {e}")


@app.get("/api/progress")
async def progress():
    if pool is None:
        raise HTTPException(503, "БД не подключена — прогресс недоступен")
    now = time.time()
    if _cache["payload"] is not None and now - _cache["t"] < CACHE_TTL:
        return _cache["payload"]
    async with pool.acquire() as con:
        et = await con.fetchrow(SQL_ETAG)
        etag = f"{et['wr_max']}:{et['rm_max']}:{et['rm_cnt']}"
        if _cache["payload"] is not None and etag == _cache["etag"]:
            _cache["t"] = now  # данных новых нет — продлеваем кэш без пересчёта
            return _cache["payload"]
        rows = [dict(r) for r in await con.fetch(SQL_ROWS, KM_METRICS)]
    payload = compute_progress(graph, rows)
    payload["version"] = etag
    payload["updatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _cache.update(t=now, etag=etag, payload=payload)
    return payload


# ---------------------------------------------------------------------------
# Редактирование графа с фронта (кнопка «✏️ Правка» во вьюере).
# Меняем только белый список полей; всё остальное в JSON неприкосновенно.
# ---------------------------------------------------------------------------
NODE_FIELDS = ("name", "connectionCount", "x", "y")  # x/y — перетаскивание узлов мышью
EDGE_FIELDS = ("lengthKm",)


class EditRequest(BaseModel):
    kind: Literal["node", "edge"]
    id: str
    fields: dict


def _recompute_totals(data: dict) -> tuple[float, float]:
    """Пересчитать все meta.counts после любой правки графа (длины, добавление
    и удаление объектов/связей). Проверено: пересчёт бит-в-бит совпадает со
    значениями исходного файла, поэтому «дрейфа» meta от циклов правок нет.
    Как и в исходном файле, внешние линки (externalLinks) в км-итоги не входят."""
    nodes, edges = data.get("nodes", []), data.get("edges", [])
    total = planned = 0.0
    by_type: dict[str, int] = {}
    for e in edges:
        km = e.get("lengthKm") or 0
        total += km
        if e.get("type") in ("planned", "larger_capacity"):
            planned += km
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    by_kind: dict[str, int] = {}
    by_tech: dict[str, int] = {}
    for n in nodes:
        by_kind[n["kind"]] = by_kind.get(n["kind"], 0) + 1
        if n["kind"] == "snp":
            by_tech[n.get("subtype") or "fiber"] = by_tech.get(n.get("subtype") or "fiber", 0) + 1
    counts = data.setdefault("meta", {}).setdefault("counts", {})
    counts.update(
        nodes=len(nodes), nodesByKind=by_kind, snpByTech=by_tech,
        districts=len({n["district"] for n in nodes if n.get("district")}),
        edges=len(edges), edgesByType=by_type,
        externalLinks=len(data.get("externalLinks", [])),
        totalKm=round(total, 1), plannedKm=round(planned, 1),
    )
    return counts["totalKm"], counts["plannedKm"]


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _persist(data: dict) -> tuple[float, float]:
    """Пересчитать meta.counts, атомарно сохранить мастер-JSON + fallback
    graph-data.js, перечитать граф для привязки прогресса и сбросить кэш.
    Вызывать только под _edit_lock."""
    global graph
    total_km, planned_km = _recompute_totals(data)
    _atomic_write(GRAPH_PATH, json.dumps(data, ensure_ascii=False, indent=1))
    _atomic_write(DATA_JS_PATH,
                  "window.GRAPH_DATA = "
                  + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                  + ";")
    graph = Graph(GRAPH_PATH)
    _cache.update(t=0.0, etag=None, payload=None)
    return total_km, planned_km


def _new_id(prefix: str, existing: set[str]) -> str:
    while True:
        nid = prefix + secrets.token_hex(3)
        if nid not in existing:
            return nid


def _clean_value(field: str, value):
    """Валидация значения поля из запроса; HTTPException(400) при мусоре."""
    if field == "name":
        if value is None:
            return None
        if not isinstance(value, str):
            raise HTTPException(400, "name должен быть строкой")
        value = value.strip()
        return value or None
    # координаты: обязательны и могут быть отрицательными (мировая система)
    if field in ("x", "y"):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise HTTPException(400, f"{field} должен быть числом")
        return round(float(value), 1)
    # числовые: lengthKm / connectionCount; null = «не задано»
    if value is None or value == "":
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise HTTPException(400, f"{field} должен быть числом")
    if value < 0:
        raise HTTPException(400, f"{field} не может быть отрицательным")
    if field == "connectionCount":
        return int(value)
    return round(float(value), 3)


@app.post("/api/graph/edit")
async def graph_edit(req: EditRequest):
    allowed = NODE_FIELDS if req.kind == "node" else EDGE_FIELDS
    for f in req.fields:
        if f not in allowed:
            raise HTTPException(400, f"поле «{f}» менять нельзя (разрешены: {', '.join(allowed)})")
    if not req.fields:
        raise HTTPException(400, "нет полей для изменения")

    async with _edit_lock:
        data = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        coll = data["nodes"] if req.kind == "node" else data["edges"]
        item = next((x for x in coll if x.get("id") == req.id), None)
        if item is None:
            raise HTTPException(404, f"{req.kind} {req.id} не найден в графе")

        applied = {}
        for f, v in req.fields.items():
            applied[f] = item[f] = _clean_value(f, v)
        total_km, planned_km = _persist(data)

    log.info("graph edit: %s %s %s", req.kind, req.id, applied)
    return {"ok": True, "kind": req.kind, "id": req.id, "applied": applied,
            "totals": {"totalKm": total_km, "plannedKm": planned_km}}


# ---------------------------------------------------------------------------
# Добавление объектов и связей (режим «➕» во вьюере).
# ---------------------------------------------------------------------------
NODE_KINDS = ("snp", "ats", "olt", "atn", "netengine", "mufta")
SNP_SUBTYPES = ("fiber", "wifi", "starlink", "outside")
EQUIP_SUBTYPES = ("existing", "planned", "outside")
EDGE_TYPES = ("existing", "planned", "outside_project", "no_free_fibers", "larger_capacity")


class AddNodeRequest(BaseModel):
    kind: Literal["snp", "ats", "olt", "atn", "netengine", "mufta"]
    subtype: str
    name: str | None = None
    district: str | None = None
    x: float
    y: float
    connectionCount: int | None = None


class AddEdgeRequest(BaseModel):
    from_id: str
    to_id: str
    type: Literal["existing", "planned", "outside_project", "no_free_fibers", "larger_capacity"]
    lengthKm: float | None = None


class DeleteRequest(BaseModel):
    kind: Literal["node", "edge"]
    id: str


@app.post("/api/graph/add-node")
async def graph_add_node(req: AddNodeRequest):
    allowed_sub = SNP_SUBTYPES if req.kind == "snp" else EQUIP_SUBTYPES
    if req.subtype not in allowed_sub:
        raise HTTPException(400, f"подтип «{req.subtype}» недопустим для {req.kind} "
                                 f"(разрешены: {', '.join(allowed_sub)})")
    name = (req.name or "").strip() or None
    district = (req.district or "").strip() or None
    if req.connectionCount is not None and req.connectionCount < 0:
        raise HTTPException(400, "connectionCount не может быть отрицательным")

    async with _edit_lock:
        data = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        node = {
            "id": _new_id("N", {n["id"] for n in data["nodes"]}),
            "kind": req.kind, "subtype": req.subtype,
            "name": name, "district": district,
            "x": round(req.x, 1), "y": round(req.y, 1),
        }
        if req.kind == "snp" and req.connectionCount is not None:
            node["connectionCount"] = req.connectionCount
        data["nodes"].append(node)
        total_km, planned_km = _persist(data)

    log.info("graph add-node: %s", node)
    return {"ok": True, "node": node,
            "totals": {"totalKm": total_km, "plannedKm": planned_km}}


@app.post("/api/graph/add-edge")
async def graph_add_edge(req: AddEdgeRequest):
    if req.from_id == req.to_id:
        raise HTTPException(400, "связь должна соединять два разных узла")
    length = _clean_value("lengthKm", req.lengthKm)

    async with _edit_lock:
        data = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        node_ids = {n["id"] for n in data["nodes"]}
        for nid in (req.from_id, req.to_id):
            if nid not in node_ids:
                raise HTTPException(404, f"узел {nid} не найден")
        dup = next((e for e in data["edges"]
                    if {e["from"], e["to"]} == {req.from_id, req.to_id}
                    and e["type"] == req.type), None)
        if dup is not None:
            raise HTTPException(409, f"такая связь уже есть ({dup['id']})")
        edge = {
            "id": _new_id("E", {e["id"] for e in data["edges"]}),
            "from": req.from_id, "to": req.to_id,
            "type": req.type, "lengthKm": length,
        }
        data["edges"].append(edge)
        total_km, planned_km = _persist(data)

    log.info("graph add-edge: %s", edge)
    return {"ok": True, "edge": edge,
            "totals": {"totalKm": total_km, "plannedKm": planned_km}}


@app.post("/api/graph/delete")
async def graph_delete(req: DeleteRequest):
    async with _edit_lock:
        data = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        removed_edges: list[str] = []
        if req.kind == "edge":
            before = len(data["edges"])
            data["edges"] = [e for e in data["edges"] if e["id"] != req.id]
            if len(data["edges"]) == before:
                raise HTTPException(404, f"связь {req.id} не найдена")
            removed_edges.append(req.id)
        else:
            if not any(n["id"] == req.id for n in data["nodes"]):
                raise HTTPException(404, f"узел {req.id} не найден")
            data["nodes"] = [n for n in data["nodes"] if n["id"] != req.id]
            # связи узла удаляются каскадом (включая внешние линки)
            removed_edges = [e["id"] for e in data["edges"]
                             if req.id in (e["from"], e["to"])]
            data["edges"] = [e for e in data["edges"]
                             if req.id not in (e["from"], e["to"])]
            data["externalLinks"] = [e for e in data.get("externalLinks", [])
                                     if req.id not in (e["from"], e["to"])]
        total_km, planned_km = _persist(data)

    log.info("graph delete: %s %s (снято связей: %d)", req.kind, req.id, len(removed_edges))
    return {"ok": True, "kind": req.kind, "id": req.id, "removedEdges": removed_edges,
            "totals": {"totalKm": total_km, "plannedKm": planned_km}}


# Статика (index.html, graph-app.js, zhambyl-graph.json) — ПОСЛЕ роутов API.
app.mount("/", StaticFiles(directory=BASE, html=True), name="static")
