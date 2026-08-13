/* Интерактивный вьюер графа ВОЛС (областей несколько — см. GRAPH_SLUG).
 * Данные: regions/<slug>.json (fetch) — этот путь совпадает 1:1 с реальным
 * файлом в репозитории, поэтому работает и через server.py (роут отдаёт
 * актуальную DATA_DIR-копию), и при простом file:// (браузер читает файл
 * прямо с диска, без сервера вообще). Fallback на file:// при заблокированном
 * fetch() — regions/<slug>.fallback.js (window.GRAPH_DATA_BY_SLUG[slug]).
 * Прогресс: опциональный progress.json — { "edges": { "<edgeId>": { "doneKm": 1.5 } } }
 *           или { "edges": { "<edgeId>": { "progress": 0.5 } } } (доля 0..1).
 */
(() => {
"use strict";

// ---------- Утилиты ----------
const $ = (id) => document.getElementById(id);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

// ---------- Выбор графа (области) ----------
// slug графа из ?graph=…; без параметра — дефолтный жамбылский (старые ссылки
// работают как раньше). Реестр графов живёт на сервере (GET /api/graphs).
const DEFAULT_GRAPH_SLUG = "zhambyl";
const GRAPH_SLUG = (() => {
  const p = new URLSearchParams(location.search).get("graph");
  return (p && /^[a-z0-9-]+$/.test(p)) ? p : DEFAULT_GRAPH_SLUG;
})();
const GRAPH_FILE = "regions/" + GRAPH_SLUG + ".json"; // для сообщений «сохранено в …»
// Добавить graph=<slug> к пути API — сервер по нему выбирает граф.
const graphUrl = (path) =>
  path + (path.includes("?") ? "&" : "?") + "graph=" + encodeURIComponent(GRAPH_SLUG);

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// ---------- Нормализация названий (каз ↔ рус) и нечёткий поиск ----------
const KZ2RU = {
  "ә": "а", "ғ": "г", "қ": "к", "ң": "н", "ө": "о",
  "ұ": "у", "ү": "у", "һ": "х", "і": "и", "ё": "е",
  "ъ": "", "ь": "",
  // латинские двойники → кириллица ("ATC", "M 25", "x10")
  "a": "а", "b": "в", "c": "с", "e": "е", "k": "к", "m": "м",
  "h": "н", "o": "о", "p": "р", "t": "т", "x": "х", "y": "у"
};
const PREFIX_RE = /\b(атс|atc|бтс|btc|снп|прс|рпут|рут|ррс|сш|олт|olt|ст|с|рзд|разъезд|аул|село|пос|п)\b\.?/g;

function normalizeName(s) {
  if (!s) return "";
  s = s.toLowerCase();
  s = s.replace(/[.’'"`]/g, ". ").replace(PREFIX_RE, " ");
  let out = "";
  for (const ch of s) out += (ch in KZ2RU) ? KZ2RU[ch] : ch;
  out = out.replace(/[^а-яa-z0-9 ]/g, " ");
  // Буква↔цифра без пробела («М3», «Кунбатыс2») → как в графе («М 3», «Күнбатыс 2»).
  out = out.replace(/([а-яa-z])(?=[0-9])/g, "$1 ").replace(/([0-9])(?=[а-яa-z])/g, "$1 ");
  out = out.replace(/\s+/g, " ").trim();
  return out;
}

function levenshtein(a, b) {
  if (a === b) return 0;
  const m = a.length, n = b.length;
  if (!m) return n;
  if (!n) return m;
  let prev = new Array(n + 1), cur = new Array(n + 1);
  for (let j = 0; j <= n; j++) prev[j] = j;
  for (let i = 1; i <= m; i++) {
    cur[0] = i;
    for (let j = 1; j <= n; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
    }
    [prev, cur] = [cur, prev];
  }
  return prev[n];
}

/** Схожесть запроса и названия: 0..100. Порт matcher.py (двойники обязаны
 *  давать одинаковый счёт): точное совпадение > префикс > пословное включение
 *  («момышулы» ⊆ «бауыржан момышулы») > вхождение по границе слова > опечатка
 *  в 1 букву > вхождение ВНУТРИ слова («дикан» ⊂ «кызылдикан» — слабый
 *  сигнал, 55) > Левенштейн. */
function matchScore(qNorm, nameNorm) {
  if (!qNorm || !nameNorm) return 0;
  if (nameNorm === qNorm) return 100;
  if (nameNorm.startsWith(qNorm)) return 92 - Math.min(10, nameNorm.length - qNorm.length);
  const qWords = qNorm.split(" "), nWords = nameNorm.split(" ");
  const inner = qWords.length <= nWords.length ? qWords : nWords;
  const outer = inner === qWords ? nWords : qWords;
  if (inner.join("").length >= 4 && inner.every(w => outer.includes(w))) return 86;
  if (nameNorm.includes(qNorm)) {
    const esc = qNorm.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return new RegExp("(?:^| )" + esc + "(?: |$)").test(nameNorm) ? 80 : 55;
  }
  const d = levenshtein(qNorm, nameNorm);
  if (d === 1 && qNorm.length >= 4) return 90;
  if (d === 2 && qNorm.length >= 7) return 78;
  const sim = 1 - d / Math.max(qNorm.length, nameNorm.length);
  return Math.round(sim * 75);
}

// Экспорт для тестов и переиспользования (например, из консоли).
window.snpMatcher = { normalizeName, levenshtein, matchScore };

// ---------- Загрузка данных ----------
// Fallback для file:// (fetch() к локальному файлу браузер обычно блокирует
// по CORS): подключаем regions/<slug>.fallback.js тегом <script> — так
// локальные скрипты грузятся и с file://; он кладёт данные в
// window.GRAPH_DATA_BY_SLUG[slug].
function loadFallbackDataJs(slug) {
  return new Promise((resolve) => {
    const s = document.createElement("script");
    s.src = "regions/" + slug + ".fallback.js";
    s.onload = () => resolve((window.GRAPH_DATA_BY_SLUG || {})[slug] || null);
    s.onerror = () => resolve(null);
    document.head.appendChild(s);
  });
}

async function loadGraph() {
  try {
    // no-store: сервер не шлёт Cache-Control на этот файл (только Last-Modified/
    // ETag), а он меняется после каждой правки — без no-store браузер по
    // эвристике HTTP-кэша может отдать версию ДО правки на обычном F5, и
    // хуже того: обычный (не hard) reload не обязан ревалидировать fetch().
    const r = await fetch("regions/" + GRAPH_SLUG + ".json", { cache: "no-store" });
    if (r.ok) return await r.json();
  } catch (e) { /* file:// или сеть — пробуем fallback */ }
  return await loadFallbackDataJs(GRAPH_SLUG);
}

async function fetchJsonQuiet(url) {
  try {
    const r = await fetch(url, { cache: "no-store" });
    if (r.ok) return await r.json();
  } catch (e) { /* нет сервера / file:// — это штатно */ }
  return null;
}

// Прогресс: сперва живой API (server.py), иначе статичный progress.json.
async function loadProgress() {
  return (await fetchJsonQuiet(graphUrl("api/progress"))) || (await fetchJsonQuiet("progress.json"));
}

// ---------- Состояние ----------
// Узлы вне Жамбылской области, попавшие в данные с чертежа: Жартас (N0250)
// на ПДФ стоит за границей области — это Жамбылский район Алматинской обл.
// Исключение действует только на жамбылском графе.
const EXCLUDE_NODE_IDS = GRAPH_SLUG === DEFAULT_GRAPH_SLUG
  ? new Set(["N0250"]) : new Set();

let DATA = null;
let nodes = [], edges = [], nodeById = new Map();
let view = { s: 1, tx: 0, ty: 0 };      // world -> screen: sx = x*s+tx
let hover = null;                        // { kind: 'node'|'edge', obj }
let selected = null;                     // node
let selectedEdge = null;                 // ребро, открытое в панели
let selectedService = null;              // извилистая линия, открытая в панели
let selectedEdgeIds = new Set();
let editMode = false;                    // режим «✏️ Правка»: поля в панели редактируемы
let addMode = null;                      // null | "place-node" | "edge-a" | "edge-b" | "svc-a" | "svc-b"
let edgeDraftA = null, edgeDraftB = null; // узлы создаваемой связи (подсветка)
let filters = { district: "", mufta: true, existing: true, planned: true, pon: true };
let districtLabels = [];                 // { name, x, y }
let theme = null;                        // null=auto | 'light' | 'dark'
let C = {};                              // кэш цветов из CSS-переменных

// ---------- Масштаб по км («📏 по км») ----------
// Переключаемый вид: раздвигает узлы так, чтобы экранное расстояние между
// связанными объектами было пропорционально lengthKm с чертежа. Считается
// один раз пружинной релаксацией (мировые координаты не трогаются на диске —
// только n.x/n.y в памяти подменяются на пересчитанные и обратно).
const KM_WORLD_SCALE = 11; // мировых единиц на км — подобрано под текущий масштаб раскладки
let scaleMode = false;
let scaleComputed = false;

function computeScaledLayout() {
  for (const n of nodes) {
    if (n.kind === "external") continue;
    n._sx = n._ox; n._sy = n._oy;
  }
  const springEdges = edges.filter(e =>
    !e.external && e.lengthKm && e.a.kind !== "external" && e.b.kind !== "external");
  const ITER = 300;
  for (let it = 0; it < ITER; it++) {
    for (const e of springEdges) {
      const a = e.a, b = e.b;
      const dx = b._sx - a._sx, dy = b._sy - a._sy;
      const dist = Math.hypot(dx, dy) || 0.0001;
      const target = e.lengthKm * KM_WORLD_SCALE;
      const diff = (dist - target) / dist * 0.28;
      const mx = dx * diff, my = dy * diff;
      a._sx += mx; a._sy += my;
      b._sx -= mx; b._sy -= my;
    }
    for (const n of nodes) {
      if (n.kind === "external") continue;
      n._sx += (n._ox - n._sx) * 0.015;
      n._sy += (n._oy - n._sy) * 0.015;
    }
  }
  scaleComputed = true;
}

function repositionExternals() {
  for (const n of nodes) {
    if (n.kind !== "external" || !n._anchor) continue;
    n.x = n._anchor.x + 150;
    n.y = n._anchor.y - 90;
  }
}

function setScaleMode(on) {
  scaleMode = on;
  if (on) {
    if (!scaleComputed) computeScaledLayout();
    for (const n of nodes) {
      if (n.kind === "external") continue;
      n.x = n._sx; n.y = n._sy;
    }
  } else {
    for (const n of nodes) {
      if (n.kind === "external") continue;
      n.x = n._ox; n.y = n._oy;
    }
  }
  repositionExternals();
  computeDistrictLabels();
  fitView();
}

const canvas = $("canvas");
const ctx = canvas.getContext("2d");
const tooltip = $("tooltip");

const KIND_RU = {
  snp: "СНП", ats: "АТС", olt: "OLT", atn: "ATN (транзит)",
  netengine: "NetEngine", mufta: "муфта", external: "внешняя область"
};
const TECH_RU = { fiber: "оптика", wifi: "Wi-Fi", starlink: "Starlink", outside: "вне проекта" };
const EDGE_RU = {
  existing: "существующая ВОЛС", planned: "ВОЛС по проекту",
  outside_project: "ВОЛС вне проекта", no_free_fibers: "ВОЛС без свободных ОВ",
  larger_capacity: "ВОЛС большей ёмкости"
};

function refreshColors() {
  C = {
    surface: cssVar("--surface-1"), page: cssVar("--page"),
    text: cssVar("--text-primary"), textSec: cssVar("--text-secondary"), textMuted: cssVar("--text-muted"),
    grid: cssVar("--grid"),
    snp: { fiber: cssVar("--snp-fiber"), wifi: cssVar("--snp-wifi"), starlink: cssVar("--snp-starlink"), outside: cssVar("--snp-outside") },
    ats: cssVar("--ats"), olt: cssVar("--olt"), atn: cssVar("--atn"),
    oltPlanned: cssVar("--olt-planned"), atnPlanned: cssVar("--atn-planned"),
    muftaPlanned: cssVar("--mufta-planned"), muftaOutside: cssVar("--mufta-outside"),
    netengine: cssVar("--netengine"), mufta: cssVar("--mufta"), external: cssVar("--external"),
    edge: {
      existing: cssVar("--edge-existing"), planned: cssVar("--edge-planned"),
      outside_project: cssVar("--edge-outside"), no_free_fibers: cssVar("--edge-nofree"),
      larger_capacity: cssVar("--edge-planned")
    },
    progress: cssVar("--progress") || "#0ca30c",
    pon: cssVar("--pon") || "#00b0f0",
    uplink: cssVar("--uplink") || "#00b050",
    accent: cssVar("--accent")
  };
}

function nodeColor(n) {
  if (n.kind === "snp") return C.snp[n.subtype] || C.snp.fiber;
  if (n.kind === "olt") return n.subtype === "planned" ? C.oltPlanned : C.olt;
  if (n.kind === "atn") return n.subtype === "planned" ? C.atnPlanned : C.atn;
  if (n.kind === "mufta") {
    if (n.subtype === "planned") return C.muftaPlanned;
    if (n.subtype === "outside") return C.muftaOutside;
    return C.mufta;
  }
  return C[n.kind] || C.mufta;
}

function nodeRadius(n) {
  if (n.kind === "mufta") return 4.5;
  if (n.kind === "snp") return 7 + Math.min(5, Math.sqrt(n.connectionCount || 0) / 2.6);
  if (n.kind === "external") return 5;
  return 6;
}

const PLANNED_EDGE_TYPES = new Set(["planned", "larger_capacity"]);

/** Доля выполнения ребра 0..1 или null.
 *  Прогресс-бар ОДИН на все источники факта: сервер уже свёл в doneKm и свод
 *  СМР, и применённые модератором отчёты бота (см. _build_progress в
 *  server.py). Фронту здесь различать источники не нужно и не следует —
 *  двухцветная разметка одной и той же линии оказалась нечитаемой.
 *  Если известны и doneKm, и lengthKm — обычная дробь. Если lengthKm не задан
 *  на чертеже (см. progress_core.py), а doneKm есть — км всё равно засчитаны
 *  (не обрезаны), но дроби без знаменателя не существует, поэтому просто
 *  рисуем ребро как выполненное целиком (fraction=1). */
function edgeFraction(e) {
  // Прогресс имеет смысл только на плановых линиях: существующая ВОЛС
  // уже построена, работы там не ведутся — бар не рисуем.
  if (!PLANNED_EDGE_TYPES.has(e.type)) return null;
  if (e.doneKm != null) return e.lengthKm ? clamp(e.doneKm / e.lengthKm, 0, 1) : 1;
  if (e.progress != null) return clamp(e.progress, 0, 1);
  return null;
}

// ---------- Живой прогресс (поллинг API) ----------
const PROGRESS_POLL_MS = 30000;
let progressVersion = null;
let edgeById = new Map();

/** Применить payload прогресса ({edges:{id:{doneKm|progress}}, version, totals}).
 *  Возвращает true, если данные изменились и была перерисовка. */
function applyProgress(p, source) {
  if (!p || !p.edges) return false;
  if (p.version && p.version === progressVersion) { renderLive(p, source); return false; }
  progressVersion = p.version || null;
  for (const e of edges) { delete e.doneKm; delete e.progress; delete e.fillFrom; }
  for (const [eid, u] of Object.entries(p.edges)) {
    const e = edgeById.get(eid);
    if (!e) continue;
    if (u.doneKm != null) e.doneKm = u.doneKm;
    if (u.progress != null) e.progress = u.progress;
    if (u.fillFrom) e.fillFrom = u.fillFrom;
  }
  if (p.unmatched && p.unmatched.length) {
    console.warn("Отчёты без привязки к рёбрам графа:", p.unmatched);
  }
  renderLive(p, source);
  renderDebugPanel(p, source);
  // Обновить прогресс в открытой панели — но не в режиме правки,
  // иначе поллинг перерисует панель и сотрёт недописанный ввод.
  if (!editMode) {
    if (selected) openPanel(selected);
    else if (selectedEdge) openEdgePanel(selectedEdge);
  }
  draw();
  return true;
}

function renderLive(p, source) {
  const el = $("live");
  if (!el) return;
  const t = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  let txt = `⟳ ${t}`;
  if (p && p.totals) {
    if (p.totals.doneKm != null) txt += ` · выполнено ${p.totals.doneKm} км`;
    if (p.totals.reportsUnmatched) txt += ` · без привязки: ${p.totals.reportsUnmatched}`;
  }
  el.textContent = txt;
  el.style.color = "";
  el.title = source === "api" ? "Данные из БД (api/progress)" : "Статичный progress.json";
}

// ---------- Панель отчётов бота: очередь модерации ----------
// По каждому отчёту (report_id) видно: from→to как понял сервер, км+показатель
// и на какое ребро он ляжет (✅) или почему не ложится (❌).
//
// Отчёты бота НЕ попадают на прогресс-бар сами: push присылает всё подряд —
// и старое, и новое, — а привязка к участку получена фаззи-поиском и иногда
// промахивается. Поэтому здесь у каждого отчёта две кнопки: «Применить»
// (километры ложатся на граф) и «Отменить» (отчёт убирается насовсем).
// Решение хранит сервер (POST /api/graph/moderate-report), оно переживает
// рестарт и следующий push — иначе разбор очереди пришлось бы повторять
// каждые 5 минут.
let debugOpen = true;

function fmtDT(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function edgeLabel(eid) {
  const e = edgeById.get(eid);
  if (!e) return eid;
  return `${e.a.name || e.a.id} → ${e.b.name || e.b.id}`;
}

function renderDebugPanel(p, source) {
  const box = $("debugBody");
  if (!box) return;
  const trace = p.trace || [];
  const t = p.totals || {};
  let html = `<div class="dbg-summary">`
    + `обновлено ${new Date().toLocaleTimeString("ru-RU")} · источник: ${source === "api" ? "БД (api/progress)" : "progress.json"}<br>`
    + `отчётов: ${t.reportsTotal ?? "—"} · применено: ${t.reportsApproved ?? 0}`
    + ` · ждут решения: ${t.reportsPending ?? 0} · отменено: ${t.reportsRejected ?? 0}`
    + `</div>`;
  if (!trace.length) {
    html += `<div class="dbg-empty">Отчётов с километровыми показателями пока нет в БД.</div>`;
  }
  for (const e of trace) {
    const ok = e.ok === true;
    const applied = e.moderation === "approved";
    const arrow = [e.from, e.to].filter(Boolean).join(" → ") || "(нет направления)";
    html += `<div class="dbg-row ${ok ? "dbg-ok" : "dbg-fail"}${applied ? " dbg-applied" : ""}">`
      + `<div class="dbg-head"><span class="dbg-id">#${e.reportId}</span>`
      + `<span class="dbg-status">${applied ? "🟢 на графе" : ok ? "✅" : "❌"}</span></div>`
      + `<div class="dbg-arrow">${esc(arrow)}</div>`;
    if (e.district) html += `<div class="dbg-sub">район: ${esc(e.district)}</div>`;
    if (e.km != null) html += `<div class="dbg-sub">${esc(e.metric || "")}: <b>${e.km} км</b></div>`;
    if (ok) {
      const edgesTxt = (e.edges || []).map(edgeLabel).join("; ");
      html += `<div class="dbg-sub dbg-edges">→ ребро: ${esc(edgesTxt || "—")}</div>`;
    } else {
      html += `<div class="dbg-reason">${esc(e.reason || "неизвестная причина")}</div>`;
    }
    const when = e.reportedAt ? fmtDT(e.reportedAt) : fmtDT(e.submittedAt);
    if (when) html += `<div class="dbg-sub dbg-when">${when}</div>`;
    // «Применить» показываем только тем, кто вообще может лечь на граф:
    // у отчёта без привязки применять нечего — сперва надо чинить привязку.
    html += `<div class="dbg-actions">`;
    if (applied) {
      html += `<button type="button" class="dbg-btn" data-mod="reset" data-report="${e.reportId}"
                 title="Вернуть отчёт в очередь и убрать его километры с графа">↩ Вернуть</button>`;
    } else if (ok) {
      html += `<button type="button" class="dbg-btn dbg-apply" data-mod="approve" data-report="${e.reportId}"
                 title="Положить километры этого отчёта на граф">✓ Применить</button>`;
    }
    html += `<button type="button" class="dbg-btn dbg-drop" data-mod="reject" data-report="${e.reportId}"
               title="Убрать отчёт совсем: он исчезнет из очереди и с графа">✕ Отменить</button>`;
    html += `</div></div>`;
  }
  box.innerHTML = html;
  for (const btn of box.querySelectorAll("button[data-mod]")) {
    btn.addEventListener("click", () => moderateReport(
      Number(btn.dataset.report), btn.dataset.mod, btn));
  }
}

/** Решение по отчёту: сервер пересчитывает прогресс, мы перезапрашиваем.
 *  Идём через apiPost — он сам спросит пароль правки, если тот включён:
 *  модерация меняет картинку для всех, кто смотрит вьюер. */
async function moderateReport(reportId, action, btn) {
  const row = btn.closest(".dbg-row");
  if (row) row.style.opacity = "0.45";
  try {
    await apiPost("api/graph/moderate-report", { reportId, action });
    progressVersion = null;   // ответ изменился — не дать кэшу «ничего не менялось»
    await pollProgress();
  } catch (err) {
    if (row) row.style.opacity = "";
    showHint("⚠ не удалось: " + err.message);
    setTimeout(hideHint, 4000);
  }
}

function toggleDebugPanel() {
  debugOpen = !debugOpen;
  $("debugPanel").classList.toggle("open", debugOpen);
  repositionNodePanel();
}

function repositionNodePanel() {
  // Debug-панель и панель узла обе справа — не перекрываем, а сдвигаем панель
  // узла вниз, когда debug-панель открыта.
  const panel = $("panel");
  if (panel) panel.style.top = debugOpen ? "296px" : "12px";
}

function liveError() {
  const el = $("live");
  if (!el || !progressVersion) return; // ни разу не получали — молчим (file:// без API)
  el.textContent = "⚠ нет связи с API";
  el.style.color = "var(--edge-nofree)";
}

async function pollProgress() {
  const api = await fetchJsonQuiet(graphUrl("api/progress"));
  if (api) { applyProgress(api, "api"); return; }
  liveError();
}

// ---------- Подготовка данных ----------
function computeDistrictLabels() {
  const acc = new Map();
  for (const n of nodes) {
    if (!n.district || n.kind === "external") continue;
    let a = acc.get(n.district);
    if (!a) acc.set(n.district, a = { sx: 0, sy: 0, c: 0, minY: Infinity });
    a.sx += n.x; a.sy += n.y; a.c++;
    if (n.y < a.minY) a.minY = n.y;
  }
  districtLabels = [...acc.entries()].map(([name, a]) => ({
    name, x: a.sx / a.c, y: a.minY - 60
  }));
}

function prepare(data) {
  nodes = data.nodes.filter(n => !EXCLUDE_NODE_IDS.has(n.id));
  edges = data.edges.slice();
  nodeById = new Map(nodes.map(n => [n.id, n]));
  for (const n of nodes) { n._ox = n.x; n._oy = n.y; }

  // Внешние линки: псевдоузлы рядом с исходным узлом.
  for (const el of (data.externalLinks || [])) {
    const ep = el.externalEndpoint;
    if (EXCLUDE_NODE_IDS.has(el.from) || EXCLUDE_NODE_IDS.has(el.to)) continue;
    const anchorId = nodeById.has(el.from) ? el.from : el.to;
    const anchor = nodeById.get(anchorId);
    if (!anchor || !ep) continue;
    if (!nodeById.has(ep.id)) {
      const pseudo = {
        id: ep.id, kind: "external", subtype: "outside",
        name: `${ep.name} (${ep.region} обл.)`, district: ep.district,
        x: anchor.x + 150, y: anchor.y - 90, _anchor: anchor
      };
      nodes.push(pseudo);
      nodeById.set(pseudo.id, pseudo);
    }
    edges.push({ ...el, external: true });
  }

  // Ссылки на объекты узлов в рёбрах + отбрасываем битые.
  edges = edges.filter(e => {
    e.a = nodeById.get(e.from);
    e.b = nodeById.get(e.to);
    return e.a && e.b;
  });
  edgeById = new Map(edges.map(e => [e.id, e]));
  invalidateServiceLinks();

  computeDistrictLabels();

  // Индекс поиска: только именованные узлы, СНП в приоритете.
  for (const n of nodes) n._norm = normalizeName(n.name);

  // Селектор районов.
  const districts = [...new Set(nodes.map(n => n.district).filter(Boolean))].sort();
  const sel = $("districtSel");
  for (const d of districts) {
    const o = document.createElement("option");
    o.value = d; o.textContent = d;
    sel.appendChild(o);
  }
}

// ---------- Гибкие линии PON/аплинков (как на чертеже) ----------
// На чертеже от OLT к обслуживаемым сёлам тянутся тонкие извилистые голубые
// линии (PON-ветки), а зелёные извилистые — аплинк между OLT и питающим её
// существующим узлом (ATN/АТС).
//
// Рисуются ТОЛЬКО линии, нарисованные руками (режим «✏️ Правка» →
// «〰️ Линия OLT/АТС»), они лежат в DATA.serviceLinks. Автодостройки по
// топологии (ближайшая OLT по Дейкстре) больше нет: она придумывала связи,
// которых на чертеже не было. Никакой логики эти линии не несут — ни в км,
// ни в прогресс, ни в маршруты они не идут.
let serviceLinks = null; // [{ id, a, b, kind: 'pon'|'uplink', _pts }]

function invalidateServiceLinks() { serviceLinks = null; }

function computeServiceLinks() {
  serviceLinks = [];
  for (const L of (DATA && DATA.serviceLinks) || []) {
    const a = nodeById.get(L.from), b = nodeById.get(L.to);
    if (!a || !b) continue;   // узел удалён — линия просто не рисуется
    serviceLinks.push({ id: L.id, a, b, kind: L.kind === "uplink" ? "uplink" : "pon" });
  }
}

// Опорные точки извилистой кривой между узлами: вдоль прямой со знакопеременным
// поперечным смещением (форма стабильна — сеется из id). null, если узлы слиплись.
function wavyPoints(a, b) {
  const sid = a.id + "|" + b.id;
  let h = 2166136261;
  for (const ch of sid) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); }
  h >>>= 0;
  const rnd = () => { h ^= h << 13; h ^= h >>> 17; h ^= h << 5; h >>>= 0; return (h % 1024) / 1024; };
  const dx = b.x - a.x, dy = b.y - a.y;
  const len = Math.hypot(dx, dy);
  if (len < 4) return null;
  const nx = -dy / len, ny = dx / len;
  const segs = clamp(Math.round(len / 55), 2, 6);
  const amp = Math.min(24, 7 + len * 0.14);
  const pts = [[a.x, a.y]];
  for (let i = 1; i < segs; i++) {
    const t = i / segs;
    const sway = (i % 2 ? 1 : -1) * (0.45 + rnd() * 0.55) * amp;
    pts.push([a.x + dx * t + nx * sway, a.y + dy * t + ny * sway]);
  }
  pts.push([b.x, b.y]);
  return pts;
}

function strokeWavy(pts) {
  ctx.beginPath();
  const [sx, sy] = W2S(pts[0][0], pts[0][1]);
  ctx.moveTo(sx, sy);
  for (let i = 1; i < pts.length - 1; i++) {
    const [cx, cy] = W2S(pts[i][0], pts[i][1]);
    const [mx, my] = W2S((pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2);
    ctx.quadraticCurveTo(cx, cy, mx, my);
  }
  const last = pts[pts.length - 1];
  const [ex, ey] = W2S(last[0], last[1]);
  ctx.lineTo(ex, ey);
  ctx.stroke();
}

// ---------- Фильтры видимости ----------
function nodeVisible(n) {
  if (filters.district && n.district !== filters.district) return false;
  if (!filters.mufta && n.kind === "mufta") return false;
  return true;
}
function edgeTypeVisible(t) {
  if (!filters.existing && (t === "existing" || t === "no_free_fibers")) return false;
  if (!filters.planned && (t === "planned" || t === "larger_capacity")) return false;
  return true;
}
function edgeVisible(e) {
  if (!edgeTypeVisible(e.type)) return false;
  if (filters.district && e.a.district !== filters.district && e.b.district !== filters.district) return false;
  // Муфты скрыты — рёбра к ним всё равно рисуем, иначе граф разорвётся.
  return true;
}

// ---------- Вьюпорт ----------
function resize() {
  const dpr = window.devicePixelRatio || 1;
  const r = canvas.parentElement.getBoundingClientRect();
  canvas.width = Math.round(r.width * dpr);
  canvas.height = Math.round(r.height * dpr);
  canvas.style.width = r.width + "px";
  canvas.style.height = r.height + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}

function fitView() {
  const vis = nodes.filter(nodeVisible);
  if (!vis.length) return;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of vis) {
    if (n.x < minX) minX = n.x; if (n.x > maxX) maxX = n.x;
    if (n.y < minY) minY = n.y; if (n.y > maxY) maxY = n.y;
  }
  const w = canvas.clientWidth, h = canvas.clientHeight, pad = 60;
  const s = Math.min((w - pad * 2) / Math.max(1, maxX - minX), (h - pad * 2) / Math.max(1, maxY - minY));
  view.s = clamp(s, 0.02, 4);
  view.tx = w / 2 - (minX + maxX) / 2 * view.s;
  view.ty = h / 2 - (minY + maxY) / 2 * view.s;
  draw();
}

function zoomTo(n, targetScale = 1.6) {
  view.s = targetScale;
  view.tx = canvas.clientWidth / 2 - n.x * view.s;
  view.ty = canvas.clientHeight / 2 - n.y * view.s;
  draw();
}

const W2S = (x, y) => [x * view.s + view.tx, y * view.s + view.ty];
const S2W = (sx, sy) => [(sx - view.tx) / view.s, (sy - view.ty) / view.s];

// ---------- Отрисовка ----------
function edgeStyle(e) {
  switch (e.type) {
    case "existing":        return { color: C.edge.existing, width: 1.4, dash: [] };
    case "planned":         return { color: C.edge.planned, width: 1.6, dash: [7, 5] };
    case "outside_project": return { color: C.edge.outside_project, width: 1.2, dash: [2, 4] };
    case "no_free_fibers":  return { color: C.edge.no_free_fibers, width: 1.4, dash: [] };
    case "larger_capacity": return { color: C.edge.planned, width: 2.6, dash: [11, 5] };
    default:                return { color: C.edge.existing, width: 1.2, dash: [] };
  }
}

function hexPath(x, y, r) {
  for (let i = 0; i < 6; i++) {
    const a = Math.PI / 6 + i * Math.PI / 3;
    const px = x + r * 1.1 * Math.cos(a), py = y + r * 1.1 * Math.sin(a);
    if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  }
  ctx.closePath();
}

// Значки как в легенде чертежа: АТС — синий круг; ATN — прямоугольник с рамкой
// и «портами» (зелёная — существующая, красная — проектируемая); OLT — плашка
// с надписью (зелёная — существующая, голубая — планируемая); муфта — квадрат
// с буквой М/П (рамка: чёрная — сущ., жёлтая — по проекту, фиолетовая — вне
// проекта); СНП — цветной круг с числом подключений.
function drawShape(x, y, r, n) {
  const color = nodeColor(n);

  if (n.kind === "mufta") {
    const s = r * 2.1;
    ctx.beginPath();
    ctx.rect(x - s / 2, y - s / 2, s, s);
    ctx.fillStyle = C.surface;
    ctx.fill();
    ctx.lineWidth = 1.3;
    ctx.strokeStyle = color;
    ctx.stroke();
    if (view.s > 1.2) {
      ctx.font = "700 " + clamp(s * 0.8, 5, 10) + "px system-ui, sans-serif";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillStyle = color;
      ctx.fillText(n.subtype === "planned" || n.subtype === "outside" ? "П" : "М", x, y + 0.5);
      ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
    }
    return;
  }

  if (n.kind === "atn") {
    const w = r * 3.0, h = r * 1.9;
    ctx.beginPath();
    ctx.rect(x - w / 2, y - h / 2, w, h);
    ctx.fillStyle = C.surface;
    ctx.fill();
    ctx.lineWidth = 1.6;
    ctx.strokeStyle = color;
    ctx.stroke();
    if (view.s > 0.7) {
      ctx.fillStyle = color;
      const pw = w * 0.12, ph = h * 0.2;
      for (let row = 0; row < 2; row++) {
        for (let i = 0; i < 4; i++) {
          const px = x - w / 2 + w * (0.14 + i * 0.22);
          const py = y - h / 2 + h * (row ? 0.6 : 0.2);
          ctx.fillRect(px, py, pw, ph);
        }
      }
    }
    return;
  }

  if (n.kind === "olt") {
    const w = r * 2.9, h = r * 1.9;
    ctx.beginPath();
    ctx.rect(x - w / 2, y - h / 2, w, h);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.lineWidth = 1;
    ctx.strokeStyle = C.surface;
    ctx.stroke();
    if (view.s > 0.85) {
      ctx.font = "700 " + clamp(r * 0.72, 6, 9) + "px system-ui, sans-serif";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillStyle = n.subtype === "planned" ? "#10233f" : "#fff";
      ctx.fillText("OLT", x, y + 0.5);
      ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
    }
    return;
  }

  ctx.beginPath();
  if (n.kind === "netengine") hexPath(x, y, r);
  else if (n.kind === "ats") ctx.arc(x, y, r * 1.2, 0, Math.PI * 2);
  else ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
  ctx.lineWidth = 1;
  ctx.strokeStyle = C.surface;
  ctx.stroke();

  // Число плановых подключений — прямо внутри значка СНП (как на чертеже).
  if (n.kind === "snp" && n.connectionCount != null && r >= 6.5) {
    ctx.font = "700 " + clamp(r * 0.95, 7, 12) + "px system-ui, sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    // На чертеже числа чёрные; белым — только на тёмных заливках (фиолетовая).
    ctx.fillStyle = n.subtype === "outside" ? "#fff" : "#161616";
    ctx.fillText(String(n.connectionCount), x, y + 0.5);
    ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
  }
}

function labelVisible(n) {
  const s = view.s;
  if (n.kind === "snp") return s > 0.55;
  if (n.kind === "mufta") return s > 2.2;
  if (n.kind === "external") return true;
  return s > 1.0;
}

function draw() {
  if (!DATA) return;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = C.page;
  ctx.fillRect(0, 0, w, h);

  const dimmed = !!selected || !!selectedEdge || !!selectedService;
  const [wx0, wy0] = S2W(-80, -80);
  const [wx1, wy1] = S2W(w + 80, h + 80);

  // --- Подписи районов (фон) ---
  if (view.s < 1.6) {
    ctx.font = "600 " + clamp(15 / Math.sqrt(view.s), 12, 28) + "px system-ui, sans-serif";
    ctx.fillStyle = C.textMuted;
    ctx.globalAlpha = 0.55;
    ctx.textAlign = "center";
    for (const d of districtLabels) {
      if (filters.district && d.name !== filters.district) continue;
      const [sx, sy] = W2S(d.x, d.y);
      if (sx < -200 || sx > w + 200 || sy < -50 || sy > h + 50) continue;
      ctx.fillText(d.name, sx, sy);
    }
    ctx.globalAlpha = 1;
  }

  // --- Извилистые PON/аплинк-линии (под рёбрами, чтобы не мешать подписям км) ---
  if (filters.pon) {
    if (!serviceLinks) computeServiceLinks();
    ctx.setLineDash([]);
    for (const L of serviceLinks) {
      const { a, b } = L;
      // _pts — те же точки, что нарисованы: по ним же ловим клик (pickServiceLink),
      // поэтому у пропущенных линий их обнуляем, чтобы не поймать невидимую.
      L._pts = null;
      if (filters.district && a.district !== filters.district && b.district !== filters.district) continue;
      if (Math.max(a.x, b.x) < wx0 || Math.min(a.x, b.x) > wx1 ||
          Math.max(a.y, b.y) < wy0 || Math.min(a.y, b.y) > wy1) continue;
      const pts = wavyPoints(a, b);
      if (!pts) continue;
      L._pts = pts;
      const isSel = L === selectedService;
      const isHover = hover && hover.kind === "svc" && hover.obj === L;
      const active = isSel || (selected && (a === selected || b === selected));
      ctx.globalAlpha = dimmed && !active ? 0.08 : active ? 0.95 : 0.6;
      ctx.strokeStyle = L.kind === "pon" ? C.pon : C.uplink;
      ctx.lineWidth = isSel ? 2.6 : isHover ? 2.2 : active ? 1.8 : 1.1;
      strokeWavy(pts);
    }
    ctx.globalAlpha = 1;
  }

  // --- Рёбра ---
  for (const e of edges) {
    if (!edgeVisible(e)) continue;
    const { a, b } = e;
    if (Math.max(a.x, b.x) < wx0 || Math.min(a.x, b.x) > wx1 ||
        Math.max(a.y, b.y) < wy0 || Math.min(a.y, b.y) > wy1) continue;

    const [x1, y1] = W2S(a.x, a.y);
    const [x2, y2] = W2S(b.x, b.y);
    const st = edgeStyle(e);
    const isSel = selectedEdgeIds.has(e.id);
    const isHover = hover && hover.kind === "edge" && hover.obj === e;

    ctx.globalAlpha = dimmed && !isSel ? 0.15 : 1;
    ctx.strokeStyle = st.color;
    ctx.lineWidth = st.width + (isHover || isSel ? 1 : 0);
    ctx.setLineDash(st.dash);
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
    ctx.setLineDash([]);

    // Прогресс-бар поверх линии: выполненная доля — сплошная зелёная.
    // Заливка идёт от узла fillFrom (откуда физически строят), по умолчанию — от e.from.
    const frac = edgeFraction(e);
    if (frac != null && frac > 0) {
      const rev = e.fillFrom && e.fillFrom === e.to;
      ctx.strokeStyle = C.progress;
      ctx.lineWidth = st.width + 2.2;
      ctx.lineCap = "round";
      ctx.beginPath();
      const px1 = rev ? x2 : x1, py1 = rev ? y2 : y1;
      const px2 = rev ? x1 : x2, py2 = rev ? y1 : y2;
      const mx = px1 + (px2 - px1) * frac, my = py1 + (py2 - py1) * frac;
      ctx.moveTo(px1, py1);
      ctx.lineTo(mx, my);
      ctx.stroke();
      ctx.lineCap = "butt";
      if (view.s > 0.8 && e.doneKm != null) {
        // lengthKm может отсутствовать (чертёж не даёт числа для сегмента) —
        // тогда показываем только сами км без дроби «/Y», а не додумываем знаменатель.
        const label = e.lengthKm
          ? e.doneKm + "/" + e.lengthKm + " км"
          : e.doneKm + " км (длина уч. не задана)";
        ctx.font = "600 10px system-ui, sans-serif";
        ctx.textAlign = "center";
        ctx.strokeStyle = C.page; ctx.lineWidth = 3; ctx.strokeText(label, mx, my - 6);
        ctx.fillStyle = C.progress; ctx.fillText(label, mx, my - 6);
      }
    }

    // Длина участка (км) у середины линии — чтобы расстояние читалось прямо
    // с карты. Не рисуем, когда на этом же ребре уже висит подпись прогресса
    // «X/Y км», иначе цифры двоятся.
    const hasProgressLabel = frac != null && frac > 0 && e.doneKm != null && view.s > 0.8;
    if (e.lengthKm != null && view.s > 0.55 && !hasProgressLabel) {
      const lx = (x1 + x2) / 2, ly = (y1 + y2) / 2 - 4;
      ctx.font = "600 9.5px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.strokeStyle = C.page; ctx.lineWidth = 3; ctx.strokeText(e.lengthKm + " км", lx, ly);
      ctx.fillStyle = e.type === "existing" ? C.textMuted : st.color;
      ctx.fillText(e.lengthKm + " км", lx, ly);
      ctx.textAlign = "left";
    }
    ctx.globalAlpha = 1;
  }

  // --- Узлы ---
  ctx.textAlign = "left";
  for (const n of nodes) {
    if (!nodeVisible(n)) continue;
    if (n.x < wx0 || n.x > wx1 || n.y < wy0 || n.y > wy1) continue;
    const [sx, sy] = W2S(n.x, n.y);
    const r = nodeRadius(n) * clamp(Math.sqrt(view.s), 0.7, 1.6);
    const isSel = selected === n;
    const isHover = hover && hover.kind === "node" && hover.obj === n;
    const neighbor = selected && selectedEdgeIds.size &&
      edges.some(e => selectedEdgeIds.has(e.id) && (e.a === n || e.b === n));

    ctx.globalAlpha = selected && !isSel && !neighbor ? 0.25 : 1;
    drawShape(sx, sy, r + (isHover ? 1.5 : 0), n);
    if (isSel || n === edgeDraftA || n === edgeDraftB) {
      ctx.beginPath();
      ctx.arc(sx, sy, r + 5, 0, Math.PI * 2);
      ctx.strokeStyle = C.accent;
      ctx.lineWidth = 2;
      if (!isSel) ctx.setLineDash([4, 3]); // черновик связи — пунктирное кольцо
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (n.name && labelVisible(n) && (n.kind !== "mufta" || view.s > 2.2)) {
      const fs = n.kind === "snp" ? 11.5 : 10.5;
      ctx.font = (n.kind === "snp" ? "600 " : "") + fs + "px system-ui, sans-serif";
      ctx.strokeStyle = C.page;
      ctx.lineWidth = 3;
      ctx.strokeText(n.name, sx + r + 4, sy + 3.5);
      ctx.fillStyle = n.kind === "snp" ? C.text : C.textSec;
      ctx.fillText(n.name, sx + r + 4, sy + 3.5);
    }
    ctx.globalAlpha = 1;
  }

  updateStats();
}

function updateStats() {
  const m = (DATA.meta && DATA.meta.counts) || {};
  const visN = nodes.filter(nodeVisible).length;
  const visE = edges.filter(edgeVisible).length;
  let t = `узлов ${visN}/${nodes.length} · линий ${visE}/${edges.length}`;
  if (m.plannedKm != null) t += ` · план ${m.plannedKm} км из ${m.totalKm} км`;
  $("stats").textContent = t;
  renderKpi();
}

// ---------- Полоса метрик плана (км) ----------
// Две плитки: сколько оцифровано в графе (сумма lengthKm по плановым рёбрам)
// и сколько выполнено (doneKm с api/progress — свод СМР из книги «Отчет СОИ»
// плюс отчёты бота, применённые в очереди модерации). При выбранном районе
// обе цифры — по этому району; база для % выполнения — meta.plan.targetKm
// (свод), если он есть для района, иначе честно от того, что в графе.
const PLANNED_TYPES = new Set(["planned", "larger_capacity"]);
let kpiKey = null;

// Число км для плитки: без единицы (её печатает tile) и с русским разделителем.
const kpiNum = (v) => v.toLocaleString("ru-RU", { maximumFractionDigits: 1 });

function edgeInDistrict(e, d) {
  return !d || e.a.district === d || e.b.district === d;
}

function renderKpi() {
  const box = $("kpibar");
  if (!box) return;
  const plan = (DATA.meta && DATA.meta.plan) || null;
  const d = filters.district || "";

  let graphKm = 0, doneKm = 0, nEdges = 0;
  for (const e of edges) {
    if (!PLANNED_TYPES.has(e.type) || !edgeInDistrict(e, d)) continue;
    nEdges++;
    graphKm += e.lengthKm || 0;
    doneKm += e.doneKm || 0;
  }
  // Считаем СНП тем же разрезом, что и свод: оптика + Wi-Fi public — это и есть
  // 164 строки плана. Starlink и «вне проекта» в своде СМР отсутствуют.
  let nSnp = 0;
  for (const n of nodes) {
    if (n.kind !== "snp" || (d && n.district !== d)) continue;
    if (n.subtype === "fiber" || n.subtype === "wifi") nSnp++;
  }

  const targetKm = plan ? (d ? plan.byDistrict[d] : plan.targetKm) : null;
  // База для процента выполнения — свод (это план работ); если по району
  // свода нет, честно считаем от того, что оцифровано в графе.
  const base = targetKm || graphKm;

  const key = [d, targetKm, graphKm, doneKm, nEdges, nSnp].join("|");
  if (key === kpiKey) return;
  kpiKey = key;

  const tiles = [];
  tiles.push(tile("Оцифровано в графе", kpiNum(graphKm), "км",
    `${nEdges} участков · ${nSnp} СНП по проекту`, "",
    "Сумма длин плановых линий графа (planned + большей ёмкости)"));

  const pctDone = base ? clamp(doneKm / base, 0, 1) : 0;
  tiles.push(tile("Выполнено", kpiNum(doneKm), "км",
    `${kpiNum(pctDone * 100)} % от ${targetKm != null ? "свода" : "графа"} · осталось ${kpiNum(Math.max(0, base - doneKm))} км`,
    doneKm > 0 ? "ok" : "",
    "Километры, лежащие на плановых линиях: свод СМР из книги «Отчет СОИ» "
    + "плюс отчёты бота, применённые в очереди модерации (🐞 Лог)",
    pctDone));

  box.innerHTML = tiles.join("");
}

function tile(label, value, unit, sub, cls, title, barFrac) {
  return `<div class="kpi${barFrac != null ? " kpi-done" : ""}"${title ? ` title="${esc(title)}"` : ""}>`
    + `<div class="kpi-label">${esc(label)}</div>`
    + `<div class="kpi-value${cls ? " " + cls : ""}">${esc(value)}<span class="u">${esc(unit)}</span></div>`
    + `<div class="kpi-sub">${esc(sub)}</div>`
    + (barFrac != null ? `<div class="kpi-bar"><i style="width:${(barFrac * 100).toFixed(1)}%"></i></div>` : "")
    + `</div>`;
}

// ---------- Хит-тест ----------
function pickNode(sx, sy) {
  let best = null, bestD = 12;
  for (const n of nodes) {
    if (!nodeVisible(n)) continue;
    const [nx, ny] = W2S(n.x, n.y);
    const d = Math.hypot(nx - sx, ny - sy);
    const r = nodeRadius(n) * clamp(Math.sqrt(view.s), 0.7, 1.6) + 4;
    if (d < r && d < bestD) { best = n; bestD = d; }
  }
  return best;
}

function distToSegment(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1, dy = y2 - y1;
  const len2 = dx * dx + dy * dy;
  const t = len2 ? clamp(((px - x1) * dx + (py - y1) * dy) / len2, 0, 1) : 0;
  return Math.hypot(px - (x1 + dx * t), py - (y1 + dy * t));
}

function pickEdge(sx, sy) {
  let best = null, bestD = 7;
  for (const e of edges) {
    if (!edgeVisible(e)) continue;
    const [x1, y1] = W2S(e.a.x, e.a.y);
    const [x2, y2] = W2S(e.b.x, e.b.y);
    const d = distToSegment(sx, sy, x1, y1, x2, y2);
    if (d < bestD) { best = e; bestD = d; }
  }
  return best;
}

// Извилистые линии ловим по тем же точкам, что нарисованы (L._pts выставляет
// draw), иначе на изгибах промахивались бы: амплитуда волны — в мировых
// единицах и на большом зуме уходит от прямой на десятки пикселей.
function pickServiceLink(sx, sy) {
  if (!filters.pon || !serviceLinks) return null;
  let best = null, bestD = 6;
  for (const L of serviceLinks) {
    const pts = L._pts;
    if (!pts) continue;
    for (let i = 0; i < pts.length - 1; i++) {
      const [x1, y1] = W2S(pts[i][0], pts[i][1]);
      const [x2, y2] = W2S(pts[i + 1][0], pts[i + 1][1]);
      const d = distToSegment(sx, sy, x1, y1, x2, y2);
      if (d < bestD) { best = L; bestD = d; }
    }
  }
  return best;
}

// ---------- Тултип ----------
function fmtKm(v) { return v == null ? "—" : v + " км"; }

function showTooltip(px, py, html) {
  tooltip.innerHTML = html;
  tooltip.style.display = "block";
  const stage = $("stage").getBoundingClientRect();
  let x = px + 14, y = py + 14;
  if (x + tooltip.offsetWidth > stage.width - 8) x = px - tooltip.offsetWidth - 10;
  if (y + tooltip.offsetHeight > stage.height - 8) y = py - tooltip.offsetHeight - 10;
  tooltip.style.left = x + "px";
  tooltip.style.top = y + "px";
}

function nodeTooltip(n) {
  let h = `<div class="t-name">${esc(n.name || "(без названия)")}</div>`;
  h += `<div class="t-sub">${KIND_RU[n.kind] || n.kind}`;
  if (n.kind === "snp") h += ` · ${TECH_RU[n.subtype] || n.subtype}`;
  else if (n.subtype === "planned") h += " · по проекту";
  h += `</div>`;
  if (n.district) h += `<div class="t-row">${esc(n.district)}</div>`;
  if (n.connectionCount != null) h += `<div class="t-row">подключений по плану: <b>${n.connectionCount}</b></div>`;
  if (n.objectId != null) h += `<div class="t-row">objectId: ${n.objectId}</div>`;
  return h;
}

function edgeTooltip(e) {
  const frac = edgeFraction(e);
  let h = `<div class="t-name">${esc(e.a.name || e.a.id)} — ${esc(e.b.name || e.b.id)}</div>`;
  h += `<div class="t-sub">${EDGE_RU[e.type] || e.type} · ${fmtKm(e.lengthKm)}</div>`;
  if (frac != null) {
    const done = e.doneKm != null ? e.doneKm : (e.lengthKm ? Math.round(frac * e.lengthKm * 10) / 10 : null);
    h += `<div class="t-row">выполнено: <b>${done != null ? done + " км" : Math.round(frac * 100) + "%"}</b>`
       + (e.lengthKm ? ` из ${e.lengthKm} км (${Math.round(frac * 100)}%)` : "") + `</div>`;
    h += `<div class="pbar"><div style="width:${Math.round(frac * 100)}%"></div></div>`;
  }
  return h;
}

const SVC_RU = { pon: "PON-ветка от OLT", uplink: "аплинк к OLT" };

function svcTooltip(L) {
  let h = `<div class="t-name">${esc(L.a.name || L.a.id)} 〰️ ${esc(L.b.name || L.b.id)}</div>`;
  h += `<div class="t-sub">${SVC_RU[L.kind]}</div>`;
  h += `<div class="t-row">только отображение: в км и прогресс не входит</div>`;
  return h;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// ---------- Панель узла ----------
function openPanel(n) {
  selected = n;
  selectedEdge = null;
  selectedService = null;
  selectedEdgeIds = new Set(edges.filter(e => e.a === n || e.b === n).map(e => e.id));
  $("pName").textContent = n.name || "(без названия)";
  let kind = KIND_RU[n.kind] || n.kind;
  if (n.kind === "snp") kind += " · " + (TECH_RU[n.subtype] || n.subtype);
  else if (n.subtype === "planned") kind += " · по проекту";
  $("pKind").textContent = kind;

  let meta = "";
  if (n.district) meta += `<div class="p-meta">📍 ${esc(n.district)}</div>`;
  if (n.connectionCount != null) meta += `<div class="p-meta">🏠 подключений по плану: <b>${n.connectionCount}</b></div>`;
  if (n.objectId != null) meta += `<div class="p-meta">🆔 objectId: ${n.objectId}</div>`;
  meta += `<div class="p-meta" style="color:var(--text-muted)">id: ${n.id}</div>`;
  if (editMode) {
    meta += `<div class="edit-box">
      <label>Название<input type="text" id="edName" value="${esc(n.name ?? "")}"></label>`
      + (n.kind === "snp"
         ? `<label>Подключений по плану<input type="number" id="edConn" min="0" step="1" value="${n.connectionCount ?? ""}"></label>`
         : "")
      + `<button type="button" id="edSaveNode">💾 Сохранить</button>
      <button type="button" id="edDelNode" class="danger">🗑 Удалить</button>
      <div class="edit-msg" id="edMsg"></div>
    </div>`;
  }
  $("pMeta").innerHTML = meta;
  const saveBtn = $("edSaveNode");
  if (saveBtn) saveBtn.addEventListener("click", () => saveNodeEdits(n));
  const delBtn = $("edDelNode");
  if (delBtn) delBtn.addEventListener("click", () => deleteNode(n));

  $("pEdgesTitle").textContent = "Связи";
  const list = edges.filter(e => e.a === n || e.b === n);
  let h = "";
  for (const e of list) {
    const other = e.a === n ? e.b : e.a;
    const frac = edgeFraction(e);
    h += `<div class="edge-item" data-node="${other.id}" data-edge="${e.id}">`;
    h += `<div class="e-to">${esc(other.name || (KIND_RU[other.kind] + " " + other.id))}</div>`;
    h += `<div class="e-sub">${EDGE_RU[e.type] || e.type} · ${fmtKm(e.lengthKm)}`;
    if (frac != null) h += ` · выполнено ${Math.round(frac * 100)}%`;
    h += `</div>`;
    if (frac != null) h += `<div class="pbar"><div style="width:${Math.round(frac * 100)}%"></div></div>`;
    h += `</div>`;
  }

  // Извилистые линии узла — отдельными пунктами: попасть по ним мышью на карте
  // тяжело, а убирать/просматривать их удобнее прямо отсюда. При выключенном
  // слое «PON/аплинки» не показываем: правили бы то, чего не видно на карте.
  if (filters.pon) {
    if (!serviceLinks) computeServiceLinks();
    for (const L of serviceLinks.filter(x => x.a === n || x.b === n)) {
      const other = L.a === n ? L.b : L.a;
      h += `<div class="edge-item" data-svc="${esc(L.id)}">`;
      h += `<div class="e-to">〰️ ${esc(other.name || (KIND_RU[other.kind] + " " + other.id))}</div>`;
      h += `<div class="e-sub">${SVC_RU[L.kind]} · только отображение</div>`;
      h += `</div>`;
    }
  }

  $("pEdges").innerHTML = h || `<div class="p-meta">связей нет</div>`;
  $("panel").classList.add("open");
  draw();
}

// Панель ребра: инфо о сегменте ВОЛС; в режиме правки — плановая длина.
function openEdgePanel(e) {
  selected = null;
  selectedService = null;
  selectedEdge = e;
  selectedEdgeIds = new Set([e.id]);
  $("pName").textContent = `${e.a.name || e.a.id} — ${e.b.name || e.b.id}`;
  $("pKind").textContent = (EDGE_RU[e.type] || e.type) + ` · id: ${e.id}`;

  const frac = edgeFraction(e);
  let meta = `<div class="p-meta">📏 план по проекту: <b>${fmtKm(e.lengthKm)}</b></div>`;
  if (e.doneKm != null && frac != null) meta += `<div class="p-meta">🟢 выполнено: <b>${e.doneKm} км</b>`
    + (frac != null && e.lengthKm ? ` (${Math.round(frac * 100)}%)` : "") + `</div>`;
  if (frac != null) meta += `<div class="pbar"><div style="width:${Math.round(frac * 100)}%"></div></div>`;
  $("pMeta").innerHTML = meta;

  $("pEdgesTitle").textContent = editMode ? "Редактирование" : "";
  let h = "";
  if (editMode) {
    h = `<div class="edit-box">
      <label>Плановая длина, км<input type="number" id="edLen" min="0" step="0.1"
        value="${e.lengthKm ?? ""}" placeholder="не задана на чертеже"></label>
      <button type="button" id="edSaveEdge">💾 Сохранить</button>
      <button type="button" id="edDelEdge" class="danger">🗑 Удалить связь</button>
      <div class="edit-msg" id="edMsg"></div>
    </div>`;
  }
  $("pEdges").innerHTML = h;
  const saveBtn = $("edSaveEdge");
  if (saveBtn) saveBtn.addEventListener("click", () => saveEdgeEdits(e));
  const delBtn = $("edDelEdge");
  if (delBtn) delBtn.addEventListener("click", () => deleteEdge(e));
  $("panel").classList.add("open");
  draw();
}

// Панель извилистой линии: она декоративная, поэтому единственное действие —
// убрать её (в режиме правки).
function openServiceLinkPanel(L) {
  selected = null;
  selectedEdge = null;
  selectedService = L;
  selectedEdgeIds = new Set();
  $("pName").textContent = `${L.a.name || L.a.id} 〰️ ${L.b.name || L.b.id}`;
  $("pKind").textContent = SVC_RU[L.kind];
  $("pMeta").innerHTML = `<div class="p-meta">Линия только для отображения — `
    + `на км, прогресс и маршруты не влияет.</div>`;
  $("pEdgesTitle").textContent = editMode ? "Редактирование" : "";
  $("pEdges").innerHTML = editMode
    ? `<div class="edit-box">
        <button type="button" id="edDelSvc" class="danger">🗑 Убрать линию</button>
        <div class="edit-msg" id="edMsg"></div>
      </div>`
    : "";
  const delBtn = $("edDelSvc");
  if (delBtn) delBtn.addEventListener("click", () => deleteServiceLink(L));
  $("panel").classList.add("open");
  draw();
}

function closePanel() {
  selected = null;
  selectedEdge = null;
  selectedService = null;
  selectedEdgeIds = new Set();
  $("panel").classList.remove("open");
  draw();
}

// ---------- Редактирование графа (автосохранение в JSON текущего графа) ----------
// Сессия правки: если сервер запущен с EDIT_PASSWORD (см. server.py —
// публичная ссылка без другой авторизации), правка требует токен сессии,
// который выдаёт POST /api/graph/login по паролю. Токен держим в
// localStorage и шлём заголовком X-Edit-Token на каждую правку; если сервер
// ответил 401 (сессии нет/истекла), apiPost сам спрашивает пароль, логинится
// и повторяет тот же запрос — вызывающему коду ничего решать не нужно.
// Без EDIT_PASSWORD на сервере правка открыта всем — 401 никогда не придёт,
// и пароль ни разу не спросится.
const EDIT_TOKEN_KEY = "graphEditToken";
const getEditToken = () => localStorage.getItem(EDIT_TOKEN_KEY) || "";
const setEditToken = (t) => {
  if (t) localStorage.setItem(EDIT_TOKEN_KEY, t);
  else localStorage.removeItem(EDIT_TOKEN_KEY);
};

// detail от FastAPI бывает двух видов: строка (наши HTTPException) и СПИСОК
// объектов от pydantic при 422 (не тот тип поля). Без разбора списка в панель
// правки попадало «[object Object]», и причина отказа оставалась неизвестной.
function formatApiDetail(detail, fallback) {
  if (!detail) return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((e) => {
      const field = Array.isArray(e.loc) ? e.loc[e.loc.length - 1] : null;
      const msg = e.msg || e.type || JSON.stringify(e);
      return field ? `«${field}»: ${msg}` : msg;
    });
    return parts.join("; ") || fallback;
  }
  return detail.msg || JSON.stringify(detail);
}

async function graphLogin(password) {
  const r = await fetch("api/graph/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  let data = {};
  try { data = await r.json(); } catch (e) { /* не JSON */ }
  if (!r.ok) throw new Error(formatApiDetail(data.detail, r.status + " " + r.statusText));
  return data.token;
}

async function apiPost(path, body) {
  // graph=<slug> добавляется здесь, единой точкой — все правки уходят
  // в тот граф, который открыт на странице.
  const doFetch = () => fetch(graphUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Edit-Token": getEditToken() },
    body: JSON.stringify(body),
  });
  let r;
  try {
    r = await doFetch();
  } catch (e) {
    throw new Error("нет связи с server.py — изменения НЕ сохранены");
  }

  if (r.status === 401) {
    setEditToken("");
    const password = prompt("Пароль для редактирования графа:");
    if (!password) throw new Error("для правки нужен пароль");
    const token = await graphLogin(password); // бросит с текстом ошибки при неверном пароле/блокировке
    setEditToken(token);
    try {
      r = await doFetch();
    } catch (e) {
      throw new Error("нет связи с server.py — изменения НЕ сохранены");
    }
  }

  if (!r.ok) {
    // 405/404 от статики = на сервере нет этого эндпоинта → крутится старая
    // версия server.py; после git pull/правки кода его нужно перезапустить.
    if (r.status === 405 || r.status === 404) {
      throw new Error("сервер не знает этот эндпоинт — перезапустите server.py (обновите код на сервере)");
    }
    const fallback = r.status + " " + r.statusText;
    let detail = fallback;
    try { detail = formatApiDetail((await r.json()).detail, fallback); } catch (e) { /* не JSON */ }
    throw new Error(detail);
  }
  return await r.json();
}

const saveGraphEdit = (kind, id, fields) => apiPost("api/graph/edit", { kind, id, fields });

function applyTotals(resp) {
  if (resp.totals && DATA.meta && DATA.meta.counts) {
    DATA.meta.counts.totalKm = resp.totals.totalKm;
    DATA.meta.counts.plannedKm = resp.totals.plannedKm;
  }
  // Раскладка свода СМР считается от плановых линий графа (compute_smr),
  // поэтому ЛЮБАЯ правка — длина сегмента, новая связь, удаление узла —
  // делает прежний прогресс недействительным. Сервер свой кэш сбрасывает сам
  // (GraphState.reload), фронту достаточно перезапросить.
  progressVersion = null;
  pollProgress();
}

function setEditMsg(text, cls) {
  const msg = $("edMsg");
  if (msg) { msg.textContent = text; msg.className = "edit-msg " + (cls || ""); }
}

// «Подключений по плану» — это ШТУКИ дворов, целое число: сервер объявил поле
// как int и на «0.4» отвечал 422. Проверяем на месте, чтобы не гонять заведомо
// негодное значение на сервер и сказать понятным текстом, что не так.
function readConnCount(el) {
  const raw = el.value.trim();
  if (raw === "") return null;
  const v = Number(raw.replace(",", "."));
  if (!Number.isFinite(v) || !Number.isInteger(v) || v < 0) {
    throw new Error("«Подключений по плану» — целое число (сколько подключений), например 25");
  }
  return v;
}

async function saveNodeEdits(n) {
  const fields = { name: $("edName").value.trim() || null };
  const connEl = $("edConn");
  try {
    if (connEl) fields.connectionCount = readConnCount(connEl);
  } catch (err) {
    setEditMsg("⚠ " + err.message, "err");
    return;
  }
  setEditMsg("Сохраняю…");
  try {
    await saveGraphEdit("node", n.id, fields);
    n.name = fields.name;
    if ("connectionCount" in fields) n.connectionCount = fields.connectionCount;
    n._norm = normalizeName(n.name);   // чтобы поиск сразу находил новое название
    $("pName").textContent = n.name || "(без названия)";
    setEditMsg("✅ Сохранено в " + GRAPH_FILE, "ok");
    draw();
  } catch (err) {
    setEditMsg("⚠ " + err.message, "err");
  }
}

// Сохранение позиции узла после перетаскивания мышью (режим правки).
async function saveNodePosition(n) {
  try {
    const resp = await saveGraphEdit("node", n.id, {
      x: Math.round(n.x * 10) / 10,
      y: Math.round(n.y * 10) / 10,
    });
    n.x = n._ox = resp.applied.x;
    n.y = n._oy = resp.applied.y;
  } catch (err) {
    showHint("⚠ позиция не сохранена: " + err.message);
    setTimeout(hideHint, 4000);
  }
}

async function saveEdgeEdits(e) {
  const raw = $("edLen").value.trim();
  const val = raw === "" ? null : Number(raw);
  if (val != null && (!isFinite(val) || val < 0)) {
    setEditMsg("⚠ длина должна быть числом ≥ 0", "err");
    return;
  }
  setEditMsg("Сохраняю…");
  try {
    const resp = await saveGraphEdit("edge", e.id, { lengthKm: val });
    e.lengthKm = resp.applied.lengthKm;
    applyTotals(resp);
    setEditMsg("✅ Сохранено в " + GRAPH_FILE, "ok");
    updateStats();
    draw();
  } catch (err) {
    setEditMsg("⚠ " + err.message, "err");
  }
}

// ---------- Извилистые линии OLT/АТС: добавление и удаление ----------
// Сервер на каждую правку возвращает полный список линий — берём его целиком,
// чтобы фронт и файл не разъезжались.
function applyServiceLinkState(resp) {
  if (!resp || !resp.serviceLinks) return;
  DATA.serviceLinks = resp.serviceLinks;
  invalidateServiceLinks();
  draw();
}

// «Удалить все линии OLT/АТС» — это НЕ фильтр и не временное скрытие: линии
// удаляются из файла. Дальше слой пустой и наполняется заново кнопкой
// «〰️ Линия OLT/АТС».
async function clearServiceLinks() {
  if (!serviceLinks) computeServiceLinks();
  const total = serviceLinks.length;
  if (!total) {
    showHint("Линий OLT/АТС нет — рисуйте новые кнопкой «〰️ Линия OLT/АТС»");
    setTimeout(hideHint, 3500);
    return;
  }
  if (!confirm(
      `Удалить ВСЕ линии OLT/АТС (${total} шт.)?\n`
      + "Удаление окончательное: слой останется пустым, пока вы не нарисуете новые.\n"
      + "На км, прогресс и маршруты это не влияет.")) return;
  cancelAddMode();
  try {
    applyServiceLinkState(await apiPost("api/graph/clear-service-links", {}));
    closePanel();
    showHint(`🗑 Линии OLT/АТС удалены (${total}) — сохранено в ${GRAPH_FILE}`);
    setTimeout(hideHint, 4000);
  } catch (err) {
    showHint("⚠ " + err.message);
    setTimeout(hideHint, 5000);
  }
}

async function deleteServiceLink(L) {
  if (!confirm(`Убрать извилистую линию ${L.a.name || L.a.id} — ${L.b.name || L.b.id}? `
             + `Действие сохранится в JSON.`)) return;
  setEditMsg("Удаляю…");
  try {
    applyServiceLinkState(await apiPost("api/graph/delete-service-link",
                                        { from_id: L.a.id, to_id: L.b.id }));
    closePanel();
  } catch (err) {
    setEditMsg("⚠ " + err.message, "err");
  }
}

// Форма новой извилистой линии: оба узла уже выбраны кликами.
function openAddServiceLinkForm(a, b) {
  selected = null; selectedEdge = null; selectedService = null; selectedEdgeIds = new Set();
  edgeDraftA = a; edgeDraftB = b;
  $("pName").textContent = `${a.name || a.id} 〰️ ${b.name || b.id}`;
  $("pKind").textContent = "новая извилистая линия (только отображение)";
  $("pMeta").innerHTML = `<div class="p-meta">На км, прогресс и маршруты не влияет.</div>`;
  $("pEdgesTitle").textContent = "";
  $("pEdges").innerHTML = `<div class="edit-box">
    <label>Тип линии<select id="asKind">
      <option value="pon">голубая — PON-ветка от OLT</option>
      <option value="uplink">зелёная — аплинк к OLT</option>
    </select></label>
    <button type="button" id="asCreate">〰️ Создать линию</button>
    <button type="button" id="asCancel" class="ghost">Отмена</button>
    <div class="edit-msg" id="edMsg"></div>
  </div>`;
  $("asCancel").addEventListener("click", () => { cancelAddMode(); closePanel(); });
  $("asCreate").addEventListener("click", async () => {
    setEditMsg("Сохраняю…");
    try {
      const resp = await apiPost("api/graph/add-service-link",
                                 { from_id: a.id, to_id: b.id, kind: $("asKind").value });
      edgeDraftA = edgeDraftB = null;
      applyServiceLinkState(resp);
      if (!serviceLinks) computeServiceLinks();
      const L = serviceLinks.find(x => x.id === resp.link.id);
      if (L) openServiceLinkPanel(L); else closePanel();
    } catch (err) {
      setEditMsg("⚠ " + err.message, "err");
    }
  });
  $("panel").classList.add("open");
  draw();
}

// ---------- Добавление объектов и связей, удаление ----------
function showHint(text) {
  const el = $("modeHint");
  el.textContent = text;
  el.classList.add("visible");
}
function hideHint() { $("modeHint").classList.remove("visible"); }

function cancelAddMode() {
  addMode = null;
  edgeDraftA = edgeDraftB = null;
  hideHint();
  draw();
}

function districtOptions(selectedDistrict) {
  const districts = [...new Set(nodes.map(n => n.district).filter(Boolean))].sort();
  let h = `<option value="">(без района)</option>`;
  for (const d of districts) {
    h += `<option value="${esc(d)}"${d === selectedDistrict ? " selected" : ""}>${esc(d)}</option>`;
  }
  return h;
}

const KIND_OPTIONS = [
  ["snp", "СНП (село)"], ["ats", "АТС"], ["olt", "OLT"],
  ["atn", "ATN (транзит)"], ["netengine", "NetEngine"], ["mufta", "муфта"],
];
const SNP_SUB_OPTIONS = [["fiber", "оптика"], ["wifi", "Wi-Fi"], ["starlink", "Starlink"], ["outside", "вне проекта"]];
const EQUIP_SUB_OPTIONS = [["existing", "существующий"], ["planned", "по проекту"], ["outside", "вне проекта"]];

function subtypeOptionsFor(kind) {
  const opts = kind === "snp" ? SNP_SUB_OPTIONS : EQUIP_SUB_OPTIONS;
  return opts.map(([v, t]) => `<option value="${v}">${t}</option>`).join("");
}

// Форма нового объекта: место уже выбрано кликом (wx, wy — мировые координаты).
function openAddNodeForm(wx, wy) {
  selected = null; selectedEdge = null; selectedEdgeIds = new Set();
  $("pName").textContent = "Новый объект";
  $("pKind").textContent = `координаты: ${Math.round(wx)}, ${Math.round(wy)}`;
  $("pMeta").innerHTML = "";
  $("pEdgesTitle").textContent = "";
  $("pEdges").innerHTML = `<div class="edit-box">
    <label>Тип объекта<select id="afKind">${KIND_OPTIONS.map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select></label>
    <label>Подтип<select id="afSub">${subtypeOptionsFor("snp")}</select></label>
    <label>Название<input type="text" id="afName" placeholder="например: Жаңаауыл"></label>
    <label>Район<select id="afDistrict">${districtOptions(filters.district || "")}</select></label>
    <label id="afConnRow">Подключений по плану<input type="number" id="afConn" min="0" step="1"></label>
    <button type="button" id="afCreate">➕ Создать</button>
    <button type="button" id="afCancel" class="ghost">Отмена</button>
    <div class="edit-msg" id="edMsg"></div>
  </div>`;
  $("afKind").addEventListener("change", () => {
    const k = $("afKind").value;
    $("afSub").innerHTML = subtypeOptionsFor(k);
    $("afConnRow").style.display = k === "snp" ? "" : "none";
  });
  $("afCancel").addEventListener("click", closePanel);
  $("afCreate").addEventListener("click", async () => {
    let conn;
    try {
      conn = $("afKind").value === "snp" ? readConnCount($("afConn")) : null;
    } catch (err) {
      setEditMsg("⚠ " + err.message, "err");
      return;
    }
    setEditMsg("Сохраняю…");
    try {
      const resp = await apiPost("api/graph/add-node", {
        kind: $("afKind").value,
        subtype: $("afSub").value,
        name: $("afName").value.trim() || null,
        district: $("afDistrict").value || null,
        x: wx, y: wy,
        connectionCount: conn,
      });
      const node = resp.node;
      node._norm = normalizeName(node.name);
      nodes.push(node);
      nodeById.set(node.id, node);
      applyTotals(resp);
      updateStats();
      openPanel(node); // сразу показать созданный объект (с полями правки)
      draw();
    } catch (err) {
      setEditMsg("⚠ " + err.message, "err");
    }
  });
  $("panel").classList.add("open");
  draw();
}

// Форма новой связи: оба узла уже выбраны кликами.
function openAddEdgeForm(a, b) {
  selected = null; selectedEdge = null; selectedEdgeIds = new Set();
  edgeDraftA = a; edgeDraftB = b;
  $("pName").textContent = `${a.name || a.id} — ${b.name || b.id}`;
  $("pKind").textContent = "новая связь (ВОЛС)";
  $("pMeta").innerHTML = "";
  $("pEdgesTitle").textContent = "";
  $("pEdges").innerHTML = `<div class="edit-box">
    <label>Тип ВОЛС<select id="aeType">
      <option value="planned">ВОЛС по проекту</option>
      <option value="existing">существующая ВОЛС</option>
      <option value="outside_project">ВОЛС вне проекта</option>
      <option value="no_free_fibers">без свободных ОВ</option>
      <option value="larger_capacity">большей ёмкости</option>
    </select></label>
    <label>Плановая длина, км<input type="number" id="aeLen" min="0" step="0.1" placeholder="можно оставить пустым"></label>
    <button type="button" id="aeCreate">🔗 Создать связь</button>
    <button type="button" id="aeCancel" class="ghost">Отмена</button>
    <div class="edit-msg" id="edMsg"></div>
  </div>`;
  $("aeCancel").addEventListener("click", () => { cancelAddMode(); closePanel(); });
  $("aeCreate").addEventListener("click", async () => {
    const raw = $("aeLen").value.trim();
    setEditMsg("Сохраняю…");
    try {
      const resp = await apiPost("api/graph/add-edge", {
        from_id: a.id, to_id: b.id,
        type: $("aeType").value,
        lengthKm: raw === "" ? null : Number(raw),
      });
      const edge = resp.edge;
      edge.a = nodeById.get(edge.from);
      edge.b = nodeById.get(edge.to);
      edges.push(edge);
      edgeById.set(edge.id, edge);
      invalidateServiceLinks();
      applyTotals(resp);
      updateStats();
      edgeDraftA = edgeDraftB = null;
      openEdgePanel(edge);
      draw();
    } catch (err) {
      setEditMsg("⚠ " + err.message, "err");
    }
  });
  $("panel").classList.add("open");
  draw();
}

async function deleteNode(n) {
  const cnt = edges.filter(e => e.a === n || e.b === n).length;
  const label = n.name || n.id;
  if (!confirm(`Удалить «${label}»${cnt ? ` и его связи (${cnt} шт.)` : ""}? Действие сохранится в JSON.`)) return;
  setEditMsg("Удаляю…");
  try {
    const resp = await apiPost("api/graph/delete", { kind: "node", id: n.id });
    const gone = new Set(resp.removedEdges || []);
    nodes = nodes.filter(x => x !== n);
    nodeById.delete(n.id);
    edges = edges.filter(e => !gone.has(e.id));
    for (const eid of gone) edgeById.delete(eid);
    invalidateServiceLinks();
    applyServiceLinkState(resp);   // извилистые линии узла сервер снял каскадом
    applyTotals(resp);
    updateStats();
    closePanel();
  } catch (err) {
    setEditMsg("⚠ " + err.message, "err");
  }
}

async function deleteEdge(e) {
  if (!confirm(`Удалить связь ${e.a.name || e.a.id} — ${e.b.name || e.b.id}? Действие сохранится в JSON.`)) return;
  setEditMsg("Удаляю…");
  try {
    const resp = await apiPost("api/graph/delete", { kind: "edge", id: e.id });
    edges = edges.filter(x => x !== e);
    edgeById.delete(e.id);
    invalidateServiceLinks();
    applyTotals(resp);
    updateStats();
    closePanel();
  } catch (err) {
    setEditMsg("⚠ " + err.message, "err");
  }
}

// Клики на карте в режимах добавления (вызывается из mouseup вместо выбора).
function handleAddClick(sx, sy) {
  if (addMode === "place-node") {
    const [wx, wy] = S2W(sx, sy);
    addMode = null;
    hideHint();
    openAddNodeForm(wx, wy);
    return;
  }
  const n = pickNode(sx, sy);
  if (addMode === "edge-a") {
    if (!n || n.kind === "external") return; // ждём клик именно по узлу
    edgeDraftA = n;
    addMode = "edge-b";
    showHint(`Связь: ${n.name || n.id} → кликните второй узел (Esc — отмена)`);
    draw();
    return;
  }
  if (addMode === "edge-b") {
    if (!n || n === edgeDraftA || n.kind === "external") return;
    addMode = null;
    hideHint();
    openAddEdgeForm(edgeDraftA, n);
    return;
  }
  if (addMode === "svc-a") {
    if (!n || n.kind === "external") return;
    edgeDraftA = n;
    addMode = "svc-b";
    showHint(`Извилистая линия: ${n.name || n.id} → кликните второй узел (Esc — отмена)`);
    draw();
    return;
  }
  if (addMode === "svc-b") {
    if (!n || n === edgeDraftA || n.kind === "external") return;
    addMode = null;
    hideHint();
    openAddServiceLinkForm(edgeDraftA, n);
  }
}

$("btnAddNode").addEventListener("click", () => {
  edgeDraftA = edgeDraftB = null;
  addMode = "place-node";
  closePanel();
  showHint("Кликните на карту — там появится новый объект (Esc — отмена)");
});

$("btnAddEdge").addEventListener("click", () => {
  edgeDraftA = edgeDraftB = null;
  addMode = "edge-a";
  closePanel();
  showHint("Связь: кликните ПЕРВЫЙ узел (Esc — отмена)");
});

$("btnAddSvc").addEventListener("click", () => {
  edgeDraftA = edgeDraftB = null;
  addMode = "svc-a";
  closePanel();
  // Рисовать линию при выключенном слое бессмысленно — сразу включаем его.
  if (!filters.pon) {
    filters.pon = true;
    $("fPon").checked = true;
  }
  showHint("Извилистая линия: кликните ПЕРВЫЙ узел, обычно OLT или АТС (Esc — отмена)");
});

$("btnClearSvc").addEventListener("click", clearServiceLinks);

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && addMode) cancelAddMode();
});

function setEditMode(on) {
  editMode = on;
  $("btnEdit").classList.toggle("active", editMode);
  $("btnAddNode").classList.toggle("visible", editMode);
  $("btnAddEdge").classList.toggle("visible", editMode);
  $("btnAddSvc").classList.toggle("visible", editMode);
  $("btnClearSvc").classList.toggle("visible", editMode);
  if (!editMode) cancelAddMode();
  // Правка меняет реальные x/y узлов — масштаб по км (виртуальные координаты)
  // в этот момент только мешал бы, поэтому на время правки отключаем его.
  if (editMode && scaleMode) {
    $("fScaleKm").checked = false;
    setScaleMode(false);
  }
  // перерисовать открытую панель уже с полями/без полей редактирования
  if (selected) openPanel(selected);
  else if (selectedEdge) openEdgePanel(selectedEdge);
  else if (selectedService) openServiceLinkPanel(selectedService);
}

$("btnEdit").addEventListener("click", () => setEditMode(!editMode));

$("panelClose").addEventListener("click", closePanel);
$("pEdges").addEventListener("click", (ev) => {
  const item = ev.target.closest(".edge-item");
  if (!item) return;
  if (item.dataset.svc) {
    const L = (serviceLinks || []).find(x => x.id === item.dataset.svc);
    if (L) openServiceLinkPanel(L);
    return;
  }
  const n = nodeById.get(item.dataset.node);
  if (n) { zoomTo(n, Math.max(view.s, 1.4)); openPanel(n); }
});

// ---------- Поиск ----------
const searchInput = $("search");
const searchResults = $("searchResults");
let searchItems = [];
let searchActive = -1;

function runSearch(q) {
  const qn = normalizeName(q);
  searchResults.innerHTML = "";
  searchItems = [];
  searchActive = -1;
  if (!qn || qn.length < 2) { searchResults.classList.remove("open"); return; }

  const scored = [];
  for (const n of nodes) {
    if (!n._norm) continue;
    let sc = matchScore(qn, n._norm);
    if (n.kind === "snp") sc += 6; // СНП приоритетнее оборудования с тем же названием
    if (sc >= 45) scored.push([sc, n]);
  }
  scored.sort((a, b) => b[0] - a[0]);
  const top = scored.slice(0, 9);

  if (!top.length) {
    searchResults.innerHTML = `<div class="empty">Ничего не найдено</div>`;
    searchResults.classList.add("open");
    return;
  }
  for (const [sc, n] of top) {
    const div = document.createElement("div");
    div.className = "item";
    div.innerHTML =
      `<span class="dot" style="background:${nodeColor(n)}"></span>` +
      `<span class="nm">${esc(n.name)}</span>` +
      `<span class="sub">${KIND_RU[n.kind] || n.kind}<br>${esc((n.district || "").replace(" район", ""))}</span>`;
    div.addEventListener("mousedown", (ev) => { ev.preventDefault(); selectSearch(n); });
    searchResults.appendChild(div);
    searchItems.push({ el: div, node: n });
  }
  searchResults.classList.add("open");
}

function selectSearch(n) {
  searchResults.classList.remove("open");
  searchInput.value = n.name;
  if (filters.district && n.district !== filters.district) {
    filters.district = "";
    $("districtSel").value = "";
  }
  zoomTo(n, Math.max(view.s, 1.5));
  openPanel(n);
}

searchInput.addEventListener("input", () => runSearch(searchInput.value));
searchInput.addEventListener("focus", () => runSearch(searchInput.value));
searchInput.addEventListener("blur", () => setTimeout(() => searchResults.classList.remove("open"), 150));
searchInput.addEventListener("keydown", (ev) => {
  if (!searchItems.length) return;
  if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
    ev.preventDefault();
    searchActive = (searchActive + (ev.key === "ArrowDown" ? 1 : -1) + searchItems.length) % searchItems.length;
    searchItems.forEach((it, i) => it.el.classList.toggle("active", i === searchActive));
  } else if (ev.key === "Enter") {
    selectSearch(searchItems[Math.max(0, searchActive)].node);
  } else if (ev.key === "Escape") {
    searchResults.classList.remove("open");
  }
});

// ---------- Мышь: пан/зум/ховер/перетаскивание узлов ----------
let dragging = false, dragMoved = false, lastX = 0, lastY = 0;
let dragNode = null, dragNodeMoved = false; // узел, который тащат в режиме правки

canvas.addEventListener("mousedown", (ev) => {
  lastX = ev.clientX; lastY = ev.clientY;
  // В режиме правки узел под курсором «берётся» мышью — пан карты только
  // по пустому месту. Внешние псевдоузлы не двигаем: их позиция вычисляется.
  if (editMode && !addMode) {
    const r = canvas.getBoundingClientRect();
    const n = pickNode(ev.clientX - r.left, ev.clientY - r.top);
    if (n && n.kind !== "external") {
      dragNode = n; dragNodeMoved = false;
      tooltip.style.display = "none";
      canvas.classList.add("dragging");
      return;
    }
  }
  dragging = true; dragMoved = false;
  canvas.classList.add("dragging");
});

window.addEventListener("mousemove", (ev) => {
  if (dragNode) {
    const dx = ev.clientX - lastX, dy = ev.clientY - lastY;
    if (Math.abs(dx) + Math.abs(dy) > 2) dragNodeMoved = true;
    dragNode.x += dx / view.s; dragNode.y += dy / view.s;
    dragNode._ox = dragNode.x; dragNode._oy = dragNode.y;
    lastX = ev.clientX; lastY = ev.clientY;
    repositionExternals();
    draw();
    return;
  }
  if (dragging) {
    const dx = ev.clientX - lastX, dy = ev.clientY - lastY;
    if (Math.abs(dx) + Math.abs(dy) > 2) dragMoved = true;
    view.tx += dx; view.ty += dy;
    lastX = ev.clientX; lastY = ev.clientY;
    draw();
    return;
  }
  const r = canvas.getBoundingClientRect();
  const sx = ev.clientX - r.left, sy = ev.clientY - r.top;
  if (sx < 0 || sy < 0 || sx > r.width || sy > r.height) return;
  const n = pickNode(sx, sy);
  const e = n ? null : pickEdge(sx, sy);
  // Извилистые линии — последний приоритет: они декоративные и лежат под рёбрами.
  const sv = (n || e) ? null : pickServiceLink(sx, sy);
  const newHover = n ? { kind: "node", obj: n }
    : e ? { kind: "edge", obj: e }
    : sv ? { kind: "svc", obj: sv } : null;
  const changed = JSON.stringify(hover && hover.obj.id) !== JSON.stringify(newHover && newHover.obj.id);
  hover = newHover;
  if (hover) {
    canvas.style.cursor = editMode && hover.kind === "node" ? "move" : "pointer";
    showTooltip(sx, sy, hover.kind === "node" ? nodeTooltip(hover.obj)
      : hover.kind === "svc" ? svcTooltip(hover.obj) : edgeTooltip(hover.obj));
  } else {
    canvas.style.cursor = "grab";
    tooltip.style.display = "none";
  }
  if (changed) draw();
});

window.addEventListener("mouseup", (ev) => {
  if (dragNode) {
    const n = dragNode;
    dragNode = null;
    canvas.classList.remove("dragging");
    if (dragNodeMoved) {
      scaleComputed = false;   // раскладка «📏 по км» пересчитается от новых координат
      computeDistrictLabels();
      saveNodePosition(n);
    } else {
      openPanel(n);            // клик без движения — обычное открытие панели
    }
    return;
  }
  if (!dragging) return;
  dragging = false;
  canvas.classList.remove("dragging");
  if (!dragMoved) {
    const r = canvas.getBoundingClientRect();
    const sx = ev.clientX - r.left, sy = ev.clientY - r.top;
    if (addMode) { handleAddClick(sx, sy); return; }
    const n = pickNode(sx, sy);
    if (n) { openPanel(n); return; }
    const e = pickEdge(sx, sy);
    if (e && !e.external) { openEdgePanel(e); return; }
    const sv = e ? null : pickServiceLink(sx, sy);
    if (sv) openServiceLinkPanel(sv);
    else closePanel();
  }
});

canvas.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const r = canvas.getBoundingClientRect();
  const sx = ev.clientX - r.left, sy = ev.clientY - r.top;
  const factor = Math.exp(-ev.deltaY * 0.0012);
  const ns = clamp(view.s * factor, 0.02, 8);
  const [wx, wy] = S2W(sx, sy);
  view.s = ns;
  view.tx = sx - wx * ns;
  view.ty = sy - wy * ns;
  draw();
}, { passive: false });

canvas.addEventListener("dblclick", (ev) => {
  const r = canvas.getBoundingClientRect();
  const [wx, wy] = S2W(ev.clientX - r.left, ev.clientY - r.top);
  view.s = clamp(view.s * 1.8, 0.02, 8);
  view.tx = ev.clientX - r.left - wx * view.s;
  view.ty = ev.clientY - r.top - wy * view.s;
  draw();
});

// ---------- Контролы ----------
$("btnFit").addEventListener("click", fitView);
$("districtSel").addEventListener("change", (ev) => {
  filters.district = ev.target.value;
  closePanel();
  fitView();
});
$("fMufta").addEventListener("change", (ev) => { filters.mufta = ev.target.checked; draw(); });
$("fExisting").addEventListener("change", (ev) => { filters.existing = ev.target.checked; draw(); });
$("fPlanned").addEventListener("change", (ev) => { filters.planned = ev.target.checked; draw(); });
$("fPon").addEventListener("change", (ev) => {
  filters.pon = ev.target.checked;
  // Слой выключили — панель открытой извилистой линии закрываем, а список
  // таких линий в панели узла перерисовываем (он есть только при включённом слое).
  if (!filters.pon && selectedService) closePanel();
  else if (selected) openPanel(selected);
  else draw();
});
$("fScaleKm").addEventListener("change", (ev) => {
  if (ev.target.checked && editMode) setEditMode(false);
  setScaleMode(ev.target.checked);
});

$("debugToggle").addEventListener("click", toggleDebugPanel);
$("debugClose").addEventListener("click", toggleDebugPanel);

$("btnTheme").addEventListener("click", () => {
  theme = theme === "dark" ? "light" : theme === "light" ? null : "dark";
  if (theme) document.documentElement.setAttribute("data-theme", theme);
  else document.documentElement.removeAttribute("data-theme");
  refreshColors();
  draw();
});
if (window.matchMedia) {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (!theme) { refreshColors(); draw(); }
  });
}

window.addEventListener("resize", resize);

// ---------- Селектор области (списка графов) ----------
// Список графов приходит с сервера (GET /api/graphs). Без сервера (file://)
// или с единственным графом селектор остаётся скрытым.
async function initGraphSelector() {
  const sel = $("graphSel");
  if (!sel) return;
  const list = await fetchJsonQuiet("api/graphs");
  if (!list || !Array.isArray(list.graphs) || list.graphs.length < 2) return;
  sel.innerHTML = "";
  for (const g of list.graphs) {
    const o = document.createElement("option");
    o.value = g.slug;
    o.textContent = g.title || g.region || g.slug;
    sel.appendChild(o);
  }
  sel.value = GRAPH_SLUG;
  sel.style.display = "";
  sel.addEventListener("change", () => {
    const p = new URLSearchParams(location.search);
    if (sel.value === (list.default || DEFAULT_GRAPH_SLUG)) p.delete("graph");
    else p.set("graph", sel.value);
    p.delete("district"); // фильтр района чужой области не имеет смысла
    location.search = p.toString(); // перезагрузка страницы с новым графом
  });
}

// ---------- Старт ----------
(async () => {
  const [graph, progress] = await Promise.all([loadGraph(), loadProgress()]);
  if (!graph) {
    $("error").style.display = "flex";
    return;
  }
  DATA = graph;
  // Заголовок страницы — из meta.region загруженного графа.
  const region = (graph.meta && graph.meta.region) || "";
  if (region) {
    document.title = region + " — граф ВОЛС";
    const h1 = document.querySelector("header h1");
    if (h1) h1.textContent = region + " — план ВОЛС";
  }
  initGraphSelector();
  refreshColors();
  prepare(graph);
  repositionNodePanel();
  if (progress) applyProgress(progress, progress.version ? "api" : "file");
  else renderDebugPanel({ trace: [], totals: {} }, "none");
  setInterval(pollProgress, PROGRESS_POLL_MS);

  // URL-параметры: ?district=Название — фильтр района,
  // ?theme=light|dark — принудительная тема, ?edit=1 — сразу режим правки.
  const params = new URLSearchParams(location.search);
  if (params.get("edit") === "1") setEditMode(true);
  const th = params.get("theme");
  if (th === "light" || th === "dark") {
    theme = th;
    document.documentElement.setAttribute("data-theme", th);
    refreshColors();
  }
  const d = params.get("district");
  if (d) {
    const sel = $("districtSel");
    for (const o of sel.options) {
      if (o.value === d || o.value.startsWith(d)) { sel.value = o.value; filters.district = o.value; break; }
    }
  }

  resize();
  fitView();
})();

})();
