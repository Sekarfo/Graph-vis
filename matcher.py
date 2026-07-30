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
# Буква↔цифра без пробела: работники пишут «М3», «Кунбатыс2», а в графе —
# «М 3», «Күнбатыс 2». Разделяем, чтобы обе формы нормализовались одинаково.
_ALNUM_BOUNDARY_RE = re.compile(r"(?<=[а-яa-z])(?=[0-9])|(?<=[0-9])(?=[а-яa-z])")


def normalize_name(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[.’'\"`]", ". ", s)
    s = _PREFIX_RE.sub(" ", s)
    s = s.translate(_CHAR_MAP)
    s = _NON_ALNUM_RE.sub(" ", s)
    s = _ALNUM_BOUNDARY_RE.sub(" ", s)
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
    """Схожесть нормализованных строк: 0..100 (та же шкала, что в JS).

    Ярусы подобраны по реальному промаху (#76 «Дикан»): раньше ВХОЖДЕНИЕ
    подстроки (80) побеждало ПОЧТИ ТОЧНОЕ совпадение (Левенштейн 1 давал
    лишь 60), и «Дикан» уезжал на «Қызылдикан» вместо «Дихан». Теперь:
      * вхождение внутри слова («дикан» ⊂ «кызылдикан») — слабый сигнал (55):
        случайный кусок чужого названия, а не найденное село;
      * пословное включение («момышулы» ⊆ «бауыржан момышулы») — сильный (86):
        работник дописал/опустил часть составного названия;
      * опечатка в 1 букву при названии ≥4 букв — почти точное совпадение (90).
    """
    if not q_norm or not name_norm:
        return 0
    if name_norm == q_norm:
        return 100
    if name_norm.startswith(q_norm):
        return 92 - min(10, len(name_norm) - len(q_norm))
    # Пословное включение: все слова короткой стороны есть в длинной
    # («момышулы» ⊆ «бауыржан момышулы», «аспара» ⊆ «аспара ст»). Слишком
    # короткая общая часть («м 3») не показательна — там решает вхождение ниже.
    q_words, n_words = set(q_norm.split()), set(name_norm.split())
    inner = q_words if len(q_words) <= len(n_words) else n_words
    outer = n_words if inner is q_words else q_words
    if inner <= outer and len("".join(inner)) >= 4:
        return 86
    if q_norm in name_norm:
        # По границе слова («м 3» ⊂ «муфта м 3») — сильное; внутри слова
        # («дикан» ⊂ «кызылдикан») — слабое, ниже порога привязки.
        if re.search(r"(?:^| )" + re.escape(q_norm) + r"(?: |$)", name_norm):
            return 80
        return 55
    d = levenshtein(q_norm, name_norm)
    if d == 1 and len(q_norm) >= 4:
        return 90                     # опечатка/каз-рус разночтение в 1 букву
    if d == 2 and len(q_norm) >= 7:
        return 78                     # длинное название с двумя огрехами
    sim = 1 - d / max(len(q_norm), len(name_norm))
    return round(sim * 75)


# ------------------------- районы (совместимость) -------------------------
# Названия районов в графе и в отчётах пишут по-разному: «Жуалинский» /
# «Жуалынский» / «Жуалы ауданы», «Т.Рыскулова район» / «Рыскуловский».
# Для строгого ограничителя «не класть км в чужой район» нужна проверка
# СОВМЕСТИМОСТИ, устойчивая к этим разночтениям, а не равенство строк.
_DISTRICT_NOISE_RE = re.compile(r"\b(район|ауданы|р н)\b")
_DISTRICT_SUFFIX_RE = re.compile(r"(?:йск|сск|ск)(?:ий|ая|ое)$")


def district_stem(s: str | None) -> str:
    """Основа названия района: «Жуалинский район» → «жуалин», «Жуалы ауданы»
    → «жуалы». Берётся самое длинное слово (инициалы и служебные слова —
    прочь), с него снимается прилагательное окончание."""
    n = _DISTRICT_NOISE_RE.sub(" ", normalize_name(s))
    words = n.split()
    if not words:
        return ""
    stem = _DISTRICT_SUFFIX_RE.sub("", max(words, key=len))
    return stem


def districts_compatible(a: str | None, b: str | None) -> bool:
    """Похоже, что это ОДИН И ТОТ ЖЕ район? Неизвестный район (пусто) никогда
    не блокирует — False только при явном противоречии двух известных."""
    sa, sb = district_stem(a), district_stem(b)
    if not sa or not sb:
        return True
    if sa == sb:
        return True
    t = min(len(sa), len(sb))
    if t < 4:
        return False
    # Обрезка до общей длины сглаживает окончания («жуалы»≈«жуалин»),
    # допуск в 1 букву — опечатки и каз/рус разночтения («мерки»≈«мерке»).
    return levenshtein(sa[:t], sb[:t]) <= 1
