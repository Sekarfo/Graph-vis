"""Нормализация и нечёткое сопоставление названий сёл (каз ↔ рус).

Python-порт алгоритма из graph-app.js (window.snpMatcher) — оба должны
давать одинаковый результат: «Костобе» → «Қостөбе», «Ауликоль» → «Әулиекөл».
Без внешних зависимостей.
"""
import re

# Казахские буквы → русские "как слышится" + латинские двойники → кириллица
# (в названиях встречаются "ATC", "M 25", "x10" латиницей).
_CHAR_MAP = str.maketrans({
    "ә": "а", "ғ": "г", "қ": "к", "ң": "н", "ө": "о",
    "ұ": "у", "ү": "у", "һ": "х", "і": "и", "ё": "е",
    "ъ": "", "ь": "",
    "a": "а", "b": "в", "c": "с", "e": "е", "k": "к", "m": "м",
    "h": "н", "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
})

_PREFIX_RE = re.compile(
    r"\b(атс|atc|бтс|btc|снп|прс|рпут|рут|ррс|сш|олт|olt|ст|с|рзд|разъезд|аул|село|пос|п)\b\.?"
)
_NON_ALNUM_RE = re.compile(r"[^а-яa-z0-9 ]")
_SPACES_RE = re.compile(r"\s+")


def normalize_name(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[.’'\"`]", ". ", s)
    s = _PREFIX_RE.sub(" ", s)
    s = s.translate(_CHAR_MAP)
    s = _NON_ALNUM_RE.sub(" ", s)
    return _SPACES_RE.sub(" ", s).strip()


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def match_score(q_norm: str, name_norm: str) -> int:
    """Схожесть нормализованных строк: 0..100 (та же шкала, что в JS)."""
    if not q_norm or not name_norm:
        return 0
    if name_norm == q_norm:
        return 100
    if name_norm.startswith(q_norm):
        return 92 - min(10, len(name_norm) - len(q_norm))
    if q_norm in name_norm:
        return 80
    d = levenshtein(q_norm, name_norm)
    sim = 1 - d / max(len(q_norm), len(name_norm))
    return round(sim * 75)
