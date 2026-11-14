"""Локальные проверки и нормализация вопроса: без сети, моделей и хранилищ.

Порядок и границы: docs/documents/guardrails/index.md.
"""

from __future__ import annotations

import re
import unicodedata

from rag.model_gateway.contracts import InjectionResult

# невидимые символы, которые не несут смысла в тексте вопроса, но ломают совпадение кеш-ключей
_INVISIBLE = dict.fromkeys(map(ord, "­​‌‍⁠﻿"))
_TOKEN_RE = re.compile(r"[^\W_]+")
# слово-повтор: одна буква от четырёх раз («аааа») или две чередующиеся от шести («ахахах»);
# «ок», «мама», «СССР» и короткие коды под это не попадают
_REPEAT_RE = re.compile(r"(.)\1{3,}|(.)(.)(?:\2\3){2,}\2?", re.IGNORECASE)


def _is_nonsense(text: str) -> bool:
    """Явная бессмыслица: нет ни одной буквы или цифры либо каждое слово — повтор одной-двух букв.

    Осмысленные короткие вводы (AUTH-17, короткий URL, «БО», «!> 200») бессмыслицей не считаются:
    в них есть слово, где больше двух разных символов, либо цифры.
    """
    tokens = _TOKEN_RE.findall(text)
    if not tokens:
        return True
    return all(token.isalpha() and _REPEAT_RE.fullmatch(token) for token in tokens)


def _normalize(text: str) -> str:
    """Подготовить единый текст для темы, кеша, поиска и генератора, не меняя смысл.

    Меняются только: форма Юникода (NFC), невидимые символы, управляющие символы и переводы строк
    (становятся пробелом), повторы и края пробелов. Регистр, числа, отрицания, версии, URL,
    команды и операторы сохраняются: `--force` и `--no-force`, `!> 200` и `> 200` остаются разными.
    """
    text = unicodedata.normalize("NFC", text).translate(_INVISIBLE)
    text = "".join(" " if unicodedata.category(char) in ("Cc", "Zs", "Zl", "Zp") else char for char in text)
    return " ".join(text.split())


def _is_injection(result: InjectionResult, score_thresholds: dict[str, float]) -> bool | None:
    """Решение по наблюдению детектора: True — инъекция, False — нет, None — правила для профиля нет.

    Метка модели приоритетнее оценки. Только оценка без порога для profile_id решения не даёт:
    шкала оценки определяется конкретным профилем (docs/documents/guardrails/detector.md).
    """
    if result.label is not None:
        return result.label == "injection"
    threshold = score_thresholds.get(result.profile_id)
    if threshold is None or result.score is None:
        return None
    return result.score >= threshold
