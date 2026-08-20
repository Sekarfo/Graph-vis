"""Применить пачку правок графа через вьюер (тот же путь, что кнопка «✏️ Правка»).

Зачем скрипт, если есть фронт: разбор области со сводом даёт правки десятками —
по Актюбинской только номеров муфт со схемы набралось 49. Руками это час кликов
и неизбежные опечатки, а канал нужен ТОТ ЖЕ: правку принимает сервер и пишет её
в свой DATA_DIR, откуда её потом заберёт экспорт (см. export_regions.py). Прямо
в regions/*.json писать нельзя — это зеркало прода, его затрёт следующий экспорт.

План — JSON, который можно прочитать глазами перед применением:

    {"graph": "aktobe",
     "edits": [{"kind": "node", "id": "K0166", "fields": {"name": "М4"},
                "was": "М", "why": "подпись на схеме в той же точке"}]}

Поля `was` и `why` скрипт не отправляет — они для ревью и для отката: `was`
хранит прежнее значение, и `--revert` возвращает всё как было.

Запуск:
    set EDIT_PASSWORD=...            (или --password)
    python scripts/apply_edits.py plan.json --dry-run    # показать и выйти
    python scripts/apply_edits.py plan.json              # применить
    python scripts/apply_edits.py plan.json --revert     # вернуть прежние имена
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://graph-vis-production.up.railway.app"
# Пауза между правками: сервер берёт лок на файл графа и переписывает его
# целиком на каждую правку (см. _persist в server.py) — очередь из сотни
# запросов без пауз просто держала бы этот лок.
DELAY_SEC = 0.15


def _post(url: str, body: dict, token: str) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "X-Edit-Token": token})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def login(base: str, password: str) -> str:
    """Токен сессии правки. Без EDIT_PASSWORD на сервере правка открыта всем —
    тогда токен не нужен и пустая строка сработает."""
    if not password:
        return ""
    try:
        return _post(f"{base}/api/graph/login", {"password": password}, "")["token"]
    except urllib.error.HTTPError as e:
        sys.exit(f"вьюер не принял пароль: {e.code} {e.read()[:200].decode('utf-8', 'replace')}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan", type=Path, help="JSON с правками")
    ap.add_argument("--base-url", default=os.environ.get("GRAPHVIS_URL") or DEFAULT_BASE_URL)
    ap.add_argument("--password", default=os.environ.get("EDIT_PASSWORD", ""))
    ap.add_argument("--dry-run", action="store_true", help="показать правки и выйти")
    ap.add_argument("--revert", action="store_true", help="вернуть значения из поля was")
    args = ap.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    graph = plan.get("graph")
    if not graph:
        sys.exit("в плане нет поля graph (slug области)")
    edits = plan.get("edits") or []
    if not edits:
        sys.exit("в плане нет ни одной правки")

    print(f"{'ОТКАТ' if args.revert else 'Правки'} графа «{graph}» на {args.base_url}: {len(edits)}\n")
    todo = []
    for e in edits:
        fields = dict(e["fields"])
        if args.revert:
            if "was" not in e:
                print(f"  ~ {e['id']}: нет поля was, пропускаю")
                continue
            # Откат осмыслен только для правок ОДНОГО поля — иначе непонятно,
            # какому из них принадлежит `was`.
            if len(fields) != 1:
                print(f"  ~ {e['id']}: правка на {len(fields)} полей, откат вручную")
                continue
            fields = {next(iter(fields)): e["was"]}
        shown = ", ".join(f"{k}={v!r}" for k, v in fields.items())
        print(f"  {e['id']:<10} {e.get('kind', 'node'):<5} → {shown}"
              + (f"   (было {e.get('was')!r})" if not args.revert else ""))
        todo.append((e, fields))

    if args.dry_run:
        print(f"\n--dry-run: ничего не отправлено ({len(todo)} правок готовы)")
        return

    token = login(args.base_url, args.password)
    ok = failed = 0
    for e, fields in todo:
        body = {"kind": e.get("kind", "node"), "id": e["id"], "fields": fields}
        try:
            _post(f"{args.base_url}/api/graph/edit?graph={graph}", body, token)
            ok += 1
        except urllib.error.HTTPError as err:
            failed += 1
            print(f"  ✗ {e['id']}: {err.code} {err.read()[:200].decode('utf-8', 'replace')}")
        except Exception as err:                      # noqa: BLE001 — сеть
            failed += 1
            print(f"  ✗ {e['id']}: {type(err).__name__}: {err}")
        time.sleep(DELAY_SEC)

    print(f"\nприменено {ok}, ошибок {failed}")
    if ok:
        print("Правки лежат на проде. Чтобы они уехали в репозиторий и факт "
              "пересчитался по новой геометрии — Actions → «Графы с прода + свод "
              "СНП 2.0 → факт» → Run workflow.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
