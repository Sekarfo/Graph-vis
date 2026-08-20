"""Графы с прода в репозиторий: источник правды — фронт (кнопка «✏️ Правка»).

Зачем: граф правят В БРАУЗЕРЕ, а не в редакторе. Сервер пишет правку в
`DATA_DIR/regions/<slug>.json` (см. `_persist` в server.py), и репозиторий с
этого момента — ЗЕРКАЛО прода. Обновлять зеркало надо машинно, потому что от
него зависят два потребителя:

  * раскладка факта (`scripts/import_smr2.py`) считается по ЛИНИЯМ ГРАФА и
    ключуется id рёбер. Перерисованное на фронте ребро получает НОВЫЙ id
    (`_new_id` в server.py), и факт на него не ляжет, пока свежая геометрия не
    попадёт в репозиторий — джоба берёт граф именно оттуда;
  * бот: у него `graph-vis` смонтирован `:ro` из хостового клона (см.
    docker-compose.yml), привязку `graph_from_node`/`graph_to_node` он считает
    по этим же файлам.

И это бэкап. Пока на Railway не смонтирован том (`DATA_DIR=/data`), правки
живут только в контейнере и умирают на следующем деплое — а деплой случается
на каждый коммит в main, то есть дважды в сутки от самой джобы свода.

Файл с прода НЕ берём на веру: HTTP 200, валидный JSON, `schema.validate_graph`
без ошибок, `meta.registry.slug` совпадает с именем файла, счётчики совпадают
с тем, что отдаёт `/api/graphs`, и граф не «усох» больше чем на MAX_SHRINK.
Не прошедшую проверку область НЕ пишем (остальные забираем) и выходим с кодом
1, чтобы джоба покраснела: молча положить в зеркало полупустой граф — значит
через минуту пересчитать по нему факт и потерять закраску.

Использование:
    python scripts/export_regions.py                        # с боевого вьюера
    python scripts/export_regions.py --base-url http://127.0.0.1:8000
    python scripts/export_regions.py --dry-run              # только показать
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))   # schema.py лежит рядом с server.py
sys.path.insert(0, str(HERE))          # build_fallback.py — рядом с этим файлом

from build_fallback import build_one    # noqa: E402
from schema import validate_graph       # noqa: E402

REGIONS_DIR = HERE.parent / "regions"

# Боевой вьюер. Переопределяется переменной GRAPHVIS_URL (в джобе — repo
# variable) или флагом --base-url: тот же скрипт гоняем против локального
# server.py, когда правим граф не на проде.
DEFAULT_BASE_URL = "https://graph-vis-production.up.railway.app"

# Насколько граф может «усохнуть» за один экспорт, прежде чем это перестанет
# быть правкой и станет похоже на аварию (полупустой ответ, чужой файл,
# случайное массовое удаление). 5% от текущего размера области — это десятки
# узлов: столько за один сеанс правки руками не удаляют.
MAX_SHRINK = 0.05

TIMEOUT_SEC = 90


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "export_regions"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return resp.read()


def _get_json(url: str):
    return json.loads(_get(url).decode("utf-8"))


def _dump(data: dict) -> str:
    """Ровно тот же формат, что пишет сервер (`_persist`) — иначе каждый
    экспорт давал бы дифф на весь файл от одного переформатирования."""
    return json.dumps(data, ensure_ascii=False, indent=1)


def _counts(data: dict) -> tuple[int, int]:
    return len(data.get("nodes") or []), len(data.get("edges") or [])


def export(base_url: str, *, dry_run: bool = False) -> tuple[list[str], list[str], list[str]]:
    """Забрать все области с прода. Возвращает (обновлённые, отвергнутые,
    предупреждения) — строками для отчёта в лог/step summary."""
    base = base_url.rstrip("/")
    updated: list[str] = []
    rejected: list[str] = []
    notes: list[str] = []

    try:
        listing = _get_json(f"{base}/api/graphs")
        remote = {g["slug"]: g for g in listing.get("graphs", [])}
    except (urllib.error.URLError, OSError, ValueError, KeyError, RuntimeError) as e:
        # Прод недоступен — это не повод ронять джобу: зеркало просто остаётся
        # прежним, а факт пересчитается по нему же.
        print(f"::warning::вьюер {base} не ответил на /api/graphs ({e}) — "
              f"экспорт графов пропущен")
        return updated, rejected, [f"вьюер недоступен: {e}"]

    if not remote:
        print(f"::warning::вьюер {base} не отдал ни одной области — экспорт пропущен")
        return updated, rejected, ["вьюер вернул пустой список областей"]

    local_slugs = {p.stem for p in REGIONS_DIR.glob("*.json")}
    for slug in sorted(local_slugs - set(remote)):
        # Область есть в репозитории, но вьюер её не показывает: он молча
        # пропускает файл с занятым idPrefix или битым JSON (discover_graphs).
        # Экспорт тут ни при чём, но знать об этом надо — иначе она годами
        # будет лежать в репозитории и отсутствовать на карте.
        notes.append(f"{slug}: есть в repo, но вьюер её не отдаёт (см. лог сервера)")
        print(f"::warning::regions/{slug}.json есть в репозитории, но вьюер "
              f"эту область не показывает")

    for slug in sorted(remote):
        path = REGIONS_DIR / f"{slug}.json"
        try:
            data = _get_json(f"{base}/regions/{slug}.json")
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
            rejected.append(f"{slug}: не скачался ({e})")
            print(f"::error::{slug}: не скачался ({e})")
            continue

        errors = validate_graph(data, file_slug=slug)
        if errors:
            rejected.append(f"{slug}: не прошёл схему — {errors[0]}")
            print(f"::error::{slug}: файл с прода не прошёл проверку схемы: "
                  + "; ".join(errors[:5]))
            continue

        n, e = _counts(data)
        want_n, want_e = remote[slug].get("nodes"), remote[slug].get("edges")
        if (want_n is not None and want_n != n) or (want_e is not None and want_e != e):
            # /api/graphs считает по загруженному в память графу, файл — по
            # диску. Расхождение = скачали не то (обрезанный ответ, кэш
            # прокси) либо правку записали между двумя запросами.
            rejected.append(f"{slug}: счётчики не сошлись с /api/graphs "
                            f"({n}/{e} против {want_n}/{want_e})")
            print(f"::error::{slug}: файл ({n} узлов, {e} рёбер) не сходится с "
                  f"/api/graphs ({want_n}/{want_e}) — пробуйте перезапустить джобу")
            continue

        if path.exists():
            old = json.loads(path.read_text(encoding="utf-8"))
            old_n, old_e = _counts(old)
            if n < old_n * (1 - MAX_SHRINK) or e < old_e * (1 - MAX_SHRINK):
                rejected.append(f"{slug}: подозрительная усадка "
                                f"{old_n}→{n} узлов, {old_e}→{e} рёбер")
                print(f"::error::{slug}: граф усох с {old_n}/{old_e} до {n}/{e} "
                      f"(больше {MAX_SHRINK:.0%}) — не беру. Если удаление "
                      f"настоящее, поднимите MAX_SHRINK или запишите файл руками")
                continue
            if old == data:
                continue
            what = f"узлы {old_n}→{n}, рёбра {old_e}→{e}"
        else:
            what = f"новая область: {n} узлов, {e} рёбер"

        updated.append(f"{slug}: {what}")
        if dry_run:
            print(f"[dry-run] regions/{slug}.json — {what}")
            continue
        path.write_text(_dump(data), encoding="utf-8")
        # Фоллбэк для file:// сервер держит в ногу сам, но в репозитории он
        # рядом с JSON и должен обновляться той же операцией.
        build_one(path)
        print(f"regions/{slug}.json <- {base} ({what})")

    return updated, rejected, notes


def render_summary(base_url: str, updated: list[str], rejected: list[str],
                   notes: list[str]) -> str:
    lines = [f"### Графы с прода ({base_url})", ""]
    if updated:
        lines.append("**Забрано:**")
        lines += [f"* {s}" for s in updated]
    else:
        lines.append("Правок на фронте не было — зеркало и так актуально.")
    if rejected:
        lines += ["", "**Не взято (проверки не прошли):**"]
        lines += [f"* {s}" for s in rejected]
    if notes:
        lines += ["", "**Заметки:**"]
        lines += [f"* {s}" for s in notes]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.environ.get("GRAPHVIS_URL") or DEFAULT_BASE_URL,
                    help="адрес вьюера (по умолчанию GRAPHVIS_URL или боевой)")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать, что изменилось бы, и ничего не писать")
    args = ap.parse_args()

    updated, rejected, notes = export(args.base_url, dry_run=args.dry_run)

    print()
    print(f"Обновлено областей: {len(updated)}; отвергнуто: {len(rejected)}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(render_summary(args.base_url, updated, rejected, notes))

    # Код 1 — только если прод отдал что-то, чему нельзя верить: файлы
    # остальных областей уже записаны и будут закоммичены, но джоба должна
    # покраснеть, чтобы отвергнутую область разобрали руками.
    sys.exit(1 if rejected else 0)


if __name__ == "__main__":
    main()
