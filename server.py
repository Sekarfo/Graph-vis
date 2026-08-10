"""HTTP-сервер вьюера: статика graph-vis + /api/progress
+ /api/graph/edit — редактирование графа с фронта (названия, плановые км)
с автосохранением в JSON-файл графа.

ГРАФОВ НЕСКОЛЬКО (по областям): каждый — файл regions/<slug>.json с блоком
meta.registry {slug, title, idPrefix} — сервер САМ находит все графы
сканированием REGIONS_DIR (см. discover_graphs()), никакого общего реестра
в коде нет. Это осознанное решение ради параллельной работы: если бы список
графов был хардкожен здесь одним словарём (как раньше), два человека,
добавляющие каждый свою область в один и тот же PR-поток, гарантированно
получали бы конфликт слияния на одной и той же строке. Теперь добавление
области — это только новый файл, который никого не трогает; см. CONTRIBUTING.md
и scripts/new_region.py.

Все API принимают query-параметр ?graph=<slug>; без него работает дефолтный
жамбылский граф — старые ссылки и старый фронт ничего не замечают. Список для
селектора на фронте отдаёт GET /api/graphs, файл графа — GET /graphs/<slug>.json.

Откуда берётся прогресс — ДВА источника, по ситуации:

  1. PUSH от бота (основной с 2026-07): Postgres переехал на сервер бота,
     который не принимает входящих соединений, поэтому этот сервис (Railway)
     до БД достучаться не может. Вместо этого бот сам присылает полный снимок
     строк на POST /api/progress/push с ключом X-Push-Key (сразу после
     подтверждения/правки отчёта + фоновым циклом раз в 5 минут — см.
     app/services/graphvis.py в репозитории бота). Снимок хранится в памяти и
     в DATA_DIR/progress-rows.json (переживает рестарт, если DATA_DIR — volume).
     Присланные данные ПРИОРИТЕТНЕЕ прямого доступа к БД.

  2. Прямой SQL из PostgreSQL (как раньше) — если задан DATABASE_URL и БД
     достижима (локальный запуск на той же машине, где база). Без БД и без
     push сервер тоже стартует: вьюер и редактирование графа работают,
     а /api/progress отвечает 503, пока не придёт первый push.

    export DATABASE_URL=postgresql://user:pass@host:5432/db   # или ../.env
    pip install -r requirements.txt
    uvicorn server:app --host 0.0.0.0 --port 8080

Фронт (graph-app.js) поллит GET /api/progress раз в 30 сек; ответ кэшируется
и пересчитывается только когда пришёл новый push (или, в режиме БД, когда в
БД появились новые строки — дешёвая проверка по max(id)/count).
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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from matcher import districts_compatible, normalize_name
from progress_core import KM_METRICS, Graph, compute_progress
from schema import EDGE_FIELDS, EDGE_TYPES, EQUIP_SUBTYPES, NODE_FIELDS, NODE_KINDS, SNP_SUBTYPES

log = logging.getLogger("graph-viewer")

BASE = Path(__file__).parent

# DATA_DIR — куда сохраняются правки графа (POST /api/graph/edit и т.п.).
# По умолчанию это сама папка приложения, как раньше. На Railway и подобных
# платформах файловая система контейнера эфемерна и сбрасывается на каждом
# деплое — туда стоит примонтировать persistent volume (например DATA_DIR=/data),
# иначе все правки графа будут теряться. Volume нельзя монтировать поверх BASE
# (там код), поэтому это отдельная директория; см. также явные роуты
# /zhambyl-graph.json и /graph-data.js ниже — они отдают файл из DATA_DIR,
# а не из статики BASE, иначе фронт продолжал бы видеть неизменный файл из образа.
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Графы (по областям) — АВТООБНАРУЖЕНИЕ, никакого хардкоженного реестра.
# Каждая область — файл regions/<slug>.json (код) / DATA_DIR/regions/<slug>.json
# (данные на Railway-volume) со своим блоком meta.registry {slug, title,
# idPrefix}; slug в имени файла и в meta.registry.slug обязаны совпадать —
# страховка от опечатки при копипасте шаблона. idPrefix — префикс id новых
# узлов, ОБЯЗАН быть уникален среди всех графов (иначе id из разных графов
# могли бы совпасть, а привязка graph_from_node/graph_to_node от бота работает
# по принципу «оба id есть в этом графе» — коллизия молча увела бы км в чужую
# область). Собрать новый скелет области: scripts/new_region.py; проверить
# файл перед коммитом: scripts/validate_region.py — обе команды описаны в
# CONTRIBUTING.md.
# ---------------------------------------------------------------------------
DEFAULT_GRAPH = "zhambyl"
CODE_REGIONS_DIR = BASE / "regions"
DATA_REGIONS_DIR = DATA_DIR / "regions"
DATA_REGIONS_DIR.mkdir(parents=True, exist_ok=True)


class GraphState:
    """Всё состояние одного графа: пути файлов, progress_core.Graph для
    привязки прогресса, кэш /api/progress и лок правок. Правки разных
    графов не блокируют друг друга."""

    def __init__(self, slug: str, title: str, id_prefix: str, path: Path):
        self.slug = slug
        self.title = title
        self.id_prefix = id_prefix
        self.path = path
        self.data_js_path = path.with_suffix("").with_suffix(".fallback.js")
        self.edit_lock = asyncio.Lock()
        self.cache: dict = {"t": 0.0, "etag": None, "payload": None}
        self.graph: Graph | None = None
        self.district_norms: set[str] = set()
        self.reload()

    def reload(self) -> None:
        """Перечитать граф с диска (после правки) и сбросить кэш прогресса."""
        self.graph = Graph(self.path)
        self.district_norms = {
            normalize_name(n.get("district"))
            for n in self.graph.nodes.values() if n.get("district")
        }
        self.cache.update(t=0.0, etag=None, payload=None)


def discover_graphs() -> dict[str, GraphState]:
    """Сканирует DATA_REGIONS_DIR (сидируя её из CODE_REGIONS_DIR при первом
    запуске на пустом volume) и строит {slug: GraphState} по файлам
    regions/*.json. Битый/неполный файл одной области ЛОГИРУЕТСЯ и
    пропускается — не должен ронять сервер и не должен мешать остальным
    областям грузиться (см. докстринг модуля про параллельную работу)."""
    if DATA_REGIONS_DIR != CODE_REGIONS_DIR:
        import shutil
        for src in sorted(CODE_REGIONS_DIR.glob("*.json")):
            dst = DATA_REGIONS_DIR / src.name
            if not dst.exists():
                shutil.copy(src, dst)
            js_src = src.with_suffix("").with_suffix(".fallback.js")
            js_dst = DATA_REGIONS_DIR / js_src.name
            if js_src.exists() and not js_dst.exists():
                shutil.copy(js_src, js_dst)

    states: dict[str, GraphState] = {}
    seen_prefixes: dict[str, str] = {}
    for path in sorted(DATA_REGIONS_DIR.glob("*.json")):
        file_slug = path.stem
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            reg = data["meta"]["registry"]
            slug, title, id_prefix = reg["slug"], reg["title"], reg["idPrefix"]
        except Exception as e:  # noqa: BLE001 — один плохой файл не должен ронять сервер
            log.warning("regions/%s пропущен: нет meta.registry или битый JSON (%s)",
                       path.name, e)
            continue
        if slug != file_slug:
            log.warning("regions/%s пропущен: meta.registry.slug=%r не совпадает "
                       "с именем файла", path.name, slug)
            continue
        if id_prefix in seen_prefixes:
            log.warning("regions/%s пропущен: idPrefix %r уже занят графом %r "
                       "(id новых узлов пересеклись бы)", path.name, id_prefix,
                       seen_prefixes[id_prefix])
            continue
        seen_prefixes[id_prefix] = slug
        try:
            states[slug] = GraphState(slug, title, id_prefix, path)
        except Exception:  # noqa: BLE001
            log.exception("regions/%s пропущен: граф не загрузился", path.name)
    return states


STATES: dict[str, GraphState] = discover_graphs()
if DEFAULT_GRAPH not in STATES:
    raise RuntimeError(
        f"нет рабочего графа «{DEFAULT_GRAPH}» — проверьте "
        f"regions/{DEFAULT_GRAPH}.json (scripts/validate_region.py покажет ошибку)")


def _state(graph: str) -> GraphState:
    st = STATES.get(graph)
    if st is None:
        raise HTTPException(404, f"неизвестный граф «{graph}» (есть: {', '.join(STATES)})")
    return st


def _rows_for(state: GraphState, rows: list[dict]) -> list[dict]:
    """Какие строки прогресса скармливать этому графу.

    Дефолтный (жамбылский) граф получает ВСЕ строки — ровно как до
    мультиграфа, чтобы debug-панель показывала и непривязанное. Остальным
    графам отдаём только строки их области: привязка от бота указывает на
    узлы ЭТОГО графа либо район отчёта совместим с одним из районов графа.
    Без фильтра сёла-тёзки (Жамбыл, Кайнар, Актобе есть в обеих областях)
    через фаззи-поиск клали бы километры на чужую область."""
    if state.slug == DEFAULT_GRAPH:
        return rows
    out = []
    for r in rows:
        gf, gt = r.get("graph_from_node"), r.get("graph_to_node")
        if gf and gt and gf in state.graph.nodes and gt in state.graph.nodes:
            out.append(r)
            continue
        dn = normalize_name(r.get("district_name") or r.get("object_district"))
        if dn and any(districts_compatible(dn, g) for g in state.district_norms):
            out.append(r)
    return out

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


pool: asyncpg.Pool | None = None

# ---------------------------------------------------------------------------
# Снимок прогресса, присланный ботом (POST /api/progress/push) — источник №1
# (см. докстринг модуля). Хранится и на диске: DATA_DIR на Railway — volume,
# поэтому снимок переживает рестарт и передеплой сервиса.
# ---------------------------------------------------------------------------
ROWS_PATH = DATA_DIR / "progress-rows.json"

# Общий секрет с ботом (GRAPHVIS_PUSH_KEY на его стороне). Дефолт — как у
# EDIT_PASSWORD: переменная окружения переопределяет, пустая строка не
# отключает защиту.
PUSH_KEY = os.environ.get("PUSH_KEY", "") or "1973"

_pushed: dict = {"rows": None, "at": None}  # at — ISO-время последнего push


def _load_pushed_rows() -> None:
    """Поднять последний присланный снимок с диска (после рестарта)."""
    if not ROWS_PATH.exists():
        return
    try:
        data = json.loads(ROWS_PATH.read_text(encoding="utf-8"))
        _pushed.update(rows=data["rows"], at=data.get("at"))
        log.info("Снимок прогресса загружен с диска: %d строк (push от %s)",
                 len(data["rows"]), data.get("at"))
    except Exception:  # noqa: BLE001 — битый файл не должен ронять сервер
        log.exception("Не удалось прочитать %s — ждём новый push", ROWS_PATH)


_load_pushed_rows()


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
# Пароль правки ВКЛЮЧЁН ПО УМОЛЧАНИЮ: деплой публичный (Railway), и без
# EDIT_PASSWORD в окружении правка графа была бы открыта всем. Переменная
# окружения переопределяет дефолт; полностью отключить пароль нельзя
# (пустой EDIT_PASSWORD в окружении тоже вернёт дефолт).
EDIT_PASSWORD = os.environ.get("EDIT_PASSWORD", "") or "1973"
_EDIT_PATHS = {"/api/graph/edit", "/api/graph/add-node",
               "/api/graph/add-edge", "/api/graph/delete",
               "/api/graph/add-service-link", "/api/graph/delete-service-link"}

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
    if _pushed["rows"] is not None:
        return {"ok": True, "source": "push", "pushedAt": _pushed["at"],
                "rows": len(_pushed["rows"])}
    if pool is None:
        raise HTTPException(503, "нет данных: не было push от бота, "
                                 "БД не настроена (DATABASE_URL)")
    try:
        async with pool.acquire() as con:
            await con.fetchval("SELECT 1")
        return {"ok": True, "source": "db"}
    except Exception as e:  # noqa: BLE001 — health должен отвечать, а не падать
        raise HTTPException(503, f"db: {e}")


class PushRequest(BaseModel):
    rows: list[dict]


@app.post("/api/progress/push")
async def progress_push(req: PushRequest, request: Request):
    """Приём снимка прогресса от бота (см. докстринг модуля, источник №1).

    Каждый push — ПОЛНЫЙ снимок (не дельта): просто замещаем прежний. Формат
    строк — тот же, что возвращает SQL_ROWS, только даты ISO-строками
    (compute_progress понимает их как есть). Дополнительно строки могут нести
    graph_from_node/graph_to_node — привязку участка к узлам графа, вычисленную
    ботом и подтверждённую работником: compute_progress использует её вместо
    фаззи-поиска (правило 0 в progress_core)."""
    if request.headers.get("X-Push-Key", "") != PUSH_KEY:
        raise HTTPException(401, "неверный X-Push-Key")
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _pushed.update(rows=req.rows, at=at)
    try:
        _atomic_write(ROWS_PATH, json.dumps({"at": at, "rows": req.rows},
                                            ensure_ascii=False))
    except Exception:  # noqa: BLE001 — диск только для рестартов, память важнее
        log.exception("Снимок прогресса принят, но не сохранился в %s", ROWS_PATH)
    for st in STATES.values():
        st.cache.update(t=0.0, etag=None, payload=None)
    log.info("Push прогресса от бота: %d строк", len(req.rows))
    return {"ok": True, "rows": len(req.rows), "at": at}


@app.get("/api/graphs")
async def graphs_list():
    """Список графов для селектора на фронте."""
    return {
        "default": DEFAULT_GRAPH,
        "graphs": [
            {"slug": st.slug, "title": st.title,
             "region": st.graph.meta.get("region"),
             "nodes": len(st.graph.nodes), "edges": len(st.graph.edges)}
            for st in STATES.values()
        ],
    }


@app.get("/api/progress")
async def progress(graph: str = DEFAULT_GRAPH):
    st = _state(graph)
    # Источник №1: снимок, присланный ботом, — приоритетнее прямого SQL.
    if _pushed["rows"] is not None:
        etag = f"push:{_pushed['at']}:{len(_pushed['rows'])}"
        if st.cache["payload"] is not None and st.cache["etag"] == etag:
            return st.cache["payload"]
        payload = compute_progress(st.graph, _rows_for(st, _pushed["rows"]))
        payload["version"] = etag
        payload["updatedAt"] = _pushed["at"]
        st.cache.update(t=time.time(), etag=etag, payload=payload)
        return payload

    # Источник №2: прямой SQL (локальный запуск рядом с БД, как раньше).
    if pool is None:
        raise HTTPException(503, "нет данных: не было push от бота, БД не подключена")
    now = time.time()
    if st.cache["payload"] is not None and now - st.cache["t"] < CACHE_TTL:
        return st.cache["payload"]
    async with pool.acquire() as con:
        et = await con.fetchrow(SQL_ETAG)
        etag = f"{et['wr_max']}:{et['rm_max']}:{et['rm_cnt']}"
        if st.cache["payload"] is not None and etag == st.cache["etag"]:
            st.cache["t"] = now  # данных новых нет — продлеваем кэш без пересчёта
            return st.cache["payload"]
        rows = [dict(r) for r in await con.fetch(SQL_ROWS, KM_METRICS)]
    payload = compute_progress(st.graph, _rows_for(st, rows))
    payload["version"] = etag
    payload["updatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    st.cache.update(t=now, etag=etag, payload=payload)
    return payload


# ---------------------------------------------------------------------------
# Редактирование графа с фронта (кнопка «✏️ Правка» во вьюере).
# Меняем только белый список полей (NODE_FIELDS/EDGE_FIELDS — см. schema.py,
# x/y в NODE_FIELDS — это перетаскивание узлов мышью); всё остальное в JSON
# неприкосновенно.
# ---------------------------------------------------------------------------


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


def _persist(st: GraphState, data: dict) -> tuple[float, float]:
    """Пересчитать meta.counts, атомарно сохранить мастер-JSON + fallback
    <slug>.fallback.js (для file://, все графы единообразно — см. GRAPH_DATA_BY_SLUG
    во фронте), перечитать граф для привязки прогресса и сбросить кэш.
    Вызывать только под st.edit_lock."""
    total_km, planned_km = _recompute_totals(data)
    _atomic_write(st.path, json.dumps(data, ensure_ascii=False, indent=1))
    compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    js = ("window.GRAPH_DATA_BY_SLUG = window.GRAPH_DATA_BY_SLUG || {};\n"
          f"window.GRAPH_DATA_BY_SLUG[{json.dumps(st.slug)}] = " + compact + ";")
    _atomic_write(st.data_js_path, js)
    st.reload()
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
async def graph_edit(req: EditRequest, graph: str = DEFAULT_GRAPH):
    st = _state(graph)
    allowed = NODE_FIELDS if req.kind == "node" else EDGE_FIELDS
    for f in req.fields:
        if f not in allowed:
            raise HTTPException(400, f"поле «{f}» менять нельзя (разрешены: {', '.join(allowed)})")
    if not req.fields:
        raise HTTPException(400, "нет полей для изменения")

    async with st.edit_lock:
        data = json.loads(st.path.read_text(encoding="utf-8"))
        coll = data["nodes"] if req.kind == "node" else data["edges"]
        item = next((x for x in coll if x.get("id") == req.id), None)
        if item is None:
            raise HTTPException(404, f"{req.kind} {req.id} не найден в графе")

        applied = {}
        for f, v in req.fields.items():
            applied[f] = item[f] = _clean_value(f, v)
        total_km, planned_km = _persist(st, data)

    log.info("graph edit [%s]: %s %s %s", st.slug, req.kind, req.id, applied)
    return {"ok": True, "kind": req.kind, "id": req.id, "applied": applied,
            "totals": {"totalKm": total_km, "plannedKm": planned_km}}


# ---------------------------------------------------------------------------
# Добавление объектов и связей (режим «➕» во вьюере). Допустимые kind/subtype/
# type — NODE_KINDS/SNP_SUBTYPES/EQUIP_SUBTYPES/EDGE_TYPES из schema.py.
# ---------------------------------------------------------------------------


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
async def graph_add_node(req: AddNodeRequest, graph: str = DEFAULT_GRAPH):
    st = _state(graph)
    allowed_sub = SNP_SUBTYPES if req.kind == "snp" else EQUIP_SUBTYPES
    if req.subtype not in allowed_sub:
        raise HTTPException(400, f"подтип «{req.subtype}» недопустим для {req.kind} "
                                 f"(разрешены: {', '.join(allowed_sub)})")
    name = (req.name or "").strip() or None
    district = (req.district or "").strip() or None
    if req.connectionCount is not None and req.connectionCount < 0:
        raise HTTPException(400, "connectionCount не может быть отрицательным")

    async with st.edit_lock:
        data = json.loads(st.path.read_text(encoding="utf-8"))
        node = {
            "id": _new_id(st.id_prefix, {n["id"] for n in data["nodes"]}),
            "kind": req.kind, "subtype": req.subtype,
            "name": name, "district": district,
            "x": round(req.x, 1), "y": round(req.y, 1),
        }
        if req.kind == "snp" and req.connectionCount is not None:
            node["connectionCount"] = req.connectionCount
        data["nodes"].append(node)
        total_km, planned_km = _persist(st, data)

    log.info("graph add-node [%s]: %s", st.slug, node)
    return {"ok": True, "node": node,
            "totals": {"totalKm": total_km, "plannedKm": planned_km}}


@app.post("/api/graph/add-edge")
async def graph_add_edge(req: AddEdgeRequest, graph: str = DEFAULT_GRAPH):
    st = _state(graph)
    if req.from_id == req.to_id:
        raise HTTPException(400, "связь должна соединять два разных узла")
    length = _clean_value("lengthKm", req.lengthKm)

    async with st.edit_lock:
        data = json.loads(st.path.read_text(encoding="utf-8"))
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
        total_km, planned_km = _persist(st, data)

    log.info("graph add-edge [%s]: %s", st.slug, edge)
    return {"ok": True, "edge": edge,
            "totals": {"totalKm": total_km, "plannedKm": planned_km}}


# ---------------------------------------------------------------------------
# Извилистые линии от OLT/АТС (PON-ветки и аплинки с чертежа).
# Чистое ОФОРМЛЕНИЕ: в км-итоги, привязку отчётов и маршруты (progress_core)
# они не входят вообще — фронт рисует их поверх графа.
# По умолчанию фронт достраивает их сам по топологии (Дейкстра от OLT), а
# здесь хранятся только ручные правки этой картинки:
#   data["serviceLinks"]        — линии, добавленные руками: {id, from, to, kind}
#   data["serviceLinksHidden"]  — убранные руками авто-линии: ключи "idA|idB"
#                                 (id узлов отсортированы, т.е. пара без направления)
# Удаление = «этой линии между A и B быть не должно»: снимает ручную линию и
# гасит авто-линию на той же паре. Добавление, наоборот, кладёт ручную линию и
# гасит авто на этой паре, чтобы линии не задвоились.
# ---------------------------------------------------------------------------
def _service_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


def _service_state(data: dict) -> dict:
    return {"serviceLinks": data.get("serviceLinks", []),
            "serviceLinksHidden": data.get("serviceLinksHidden", [])}


class AddServiceLinkRequest(BaseModel):
    from_id: str
    to_id: str
    kind: Literal["pon", "uplink"]


class DeleteServiceLinkRequest(BaseModel):
    from_id: str
    to_id: str


@app.post("/api/graph/add-service-link")
async def graph_add_service_link(req: AddServiceLinkRequest, graph: str = DEFAULT_GRAPH):
    st = _state(graph)
    if req.from_id == req.to_id:
        raise HTTPException(400, "линия должна соединять два разных узла")

    async with st.edit_lock:
        data = json.loads(st.path.read_text(encoding="utf-8"))
        node_ids = {n["id"] for n in data["nodes"]}
        for nid in (req.from_id, req.to_id):
            if nid not in node_ids:
                raise HTTPException(404, f"узел {nid} не найден")
        links = data.setdefault("serviceLinks", [])
        key = _service_key(req.from_id, req.to_id)
        if any(_service_key(x["from"], x["to"]) == key for x in links):
            raise HTTPException(409, "такая извилистая линия уже есть")
        link = {"id": _new_id("S", {x["id"] for x in links}),
                "from": req.from_id, "to": req.to_id, "kind": req.kind}
        links.append(link)
        hidden = data.setdefault("serviceLinksHidden", [])
        if key not in hidden:
            hidden.append(key)  # чтобы авто-линия на этой же паре не задвоилась
        _persist(st, data)
        state = _service_state(data)

    log.info("graph add-service-link [%s]: %s", st.slug, link)
    return {"ok": True, "link": link, **state}


@app.post("/api/graph/delete-service-link")
async def graph_delete_service_link(req: DeleteServiceLinkRequest, graph: str = DEFAULT_GRAPH):
    st = _state(graph)
    key = _service_key(req.from_id, req.to_id)
    async with st.edit_lock:
        data = json.loads(st.path.read_text(encoding="utf-8"))
        links = data.get("serviceLinks", [])
        removed = [x["id"] for x in links if _service_key(x["from"], x["to"]) == key]
        data["serviceLinks"] = [x for x in links
                                if _service_key(x["from"], x["to"]) != key]
        hidden = data.setdefault("serviceLinksHidden", [])
        if key not in hidden:
            hidden.append(key)
        _persist(st, data)
        state = _service_state(data)

    log.info("graph delete-service-link [%s]: %s (снято ручных: %d)", st.slug, key, len(removed))
    return {"ok": True, "removed": removed, **state}


@app.post("/api/graph/delete")
async def graph_delete(req: DeleteRequest, graph: str = DEFAULT_GRAPH):
    st = _state(graph)
    async with st.edit_lock:
        data = json.loads(st.path.read_text(encoding="utf-8"))
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
            # Извилистые линии узла уходят вместе с ним, иначе фронт будет
            # молча пропускать их как «битые» и они останутся мусором в файле.
            data["serviceLinks"] = [x for x in data.get("serviceLinks", [])
                                    if req.id not in (x["from"], x["to"])]
            data["serviceLinksHidden"] = [k for k in data.get("serviceLinksHidden", [])
                                          if req.id not in k.split("|")]
        total_km, planned_km = _persist(st, data)
        state = _service_state(data)

    log.info("graph delete [%s]: %s %s (снято связей: %d)",
             st.slug, req.kind, req.id, len(removed_edges))
    return {"ok": True, "kind": req.kind, "id": req.id, "removedEdges": removed_edges,
            "totals": {"totalKm": total_km, "plannedKm": planned_km}, **state}


# Файлы графов отдаём явно из DATA_REGIONS_DIR (см. коммент у DATA_DIR выше),
# а не из статики BASE — иначе на деплое с volume фронт видел бы неизменный
# файл из образа вместо сохранённых правок. Путь /regions/<slug>.* нарочно
# СОВПАДАЕТ с реальным относительным путём файла в репозитории — фронт ходит
# по одному и тому же URL что при запуске через server.py (тогда роут ниже
# отдаёт актуальную DATA_DIR-копию), что при простом file:// (тогда браузер
# читает этот же путь прямо с диска, без сервера вообще) — дублировать логику
# «путь для сервера» / «путь для file://» во фронте не нужно.
@app.get("/regions/{slug}.json")
async def _graph_json_by_slug(slug: str):
    return FileResponse(_state(slug).path, media_type="application/json")


@app.get("/regions/{slug}.fallback.js")
async def _graph_data_js_by_slug(slug: str):
    st = _state(slug)
    if not st.data_js_path.exists():
        raise HTTPException(404, f"нет fallback-файла {st.data_js_path.name}")
    return FileResponse(st.data_js_path, media_type="application/javascript")


# Легаси-путь дефолтного (жамбылского) графа — старые внешние ссылки на голый
# JSON (например у бота, см. app/services/graphbind.py). /regions/zhambyl.json
# выше уже покрывает то же самое; этот роут остаётся только ради обратной
# совместимости и никогда не должен удаляться без проверки, кто на него ссылается.
@app.get("/zhambyl-graph.json")
async def _graph_json_file():
    return FileResponse(STATES[DEFAULT_GRAPH].path, media_type="application/json")


# Статика (index.html, graph-app.js) — ПОСЛЕ роутов API и явных путей выше.
app.mount("/", StaticFiles(directory=BASE, html=True), name="static")
