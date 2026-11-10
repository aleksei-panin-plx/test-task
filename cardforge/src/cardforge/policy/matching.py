"""Cheap keyword filter for policy rules (first PolicyScreener pass, PromptGuard)."""

from __future__ import annotations

import re
from functools import lru_cache

_CYRILLIC = re.compile(r"[а-я]")
_RU_ENDING = re.compile(r"(ами|ями|ов|ев|ей|ой|ий|ый|ая|ое|ые|а|я|о|е|ь|й|ы|и|у|ю)$")


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


# Noun/adjective case endings only: a free suffix turned «труба» into «труп» and «атласный»
# into «атлас». Homographs such as «погоня»/«погоны» remain a known false positive.
_RU_SUFFIX = (
    "(?:а|я|о|е|ь|й|ы|и|у|ю|ой|ей|ом|ем|ам|ям|ах|ях|ами|ями|ов|ев|ью|ая|яя|ое|ее|ые|ие|ый|ий"
    "|ого|его|ому|ему|ых|их|ым|им|ыми|ими|ую|юю)?"
)


def _word_pattern(word: str) -> str:
    if _CYRILLIC.search(word):
        # Russian inflection: «скелетов», «крови», «картой» must hit «скелет», «кровь», «карта».
        stem = _RU_ENDING.sub("", word) if len(word) > 4 else word
        return re.escape(stem) + _RU_SUFFIX
    return re.escape(word) + r"(?:s|es)?"


@lru_cache(maxsize=4096)
def compile_term(term: str) -> re.Pattern[str]:
    words = normalize(term).split()
    body = r"[\s\-]+".join(_word_pattern(w) for w in words)
    return re.compile(rf"(?<!\w){body}(?!\w)")


def find_terms(text: str, terms: list[str]) -> list[str]:
    """Return the terms found in `text`, in rule order, without duplicates."""
    hay = normalize(text)
    return [t for t in dict.fromkeys(terms) if t.strip() and compile_term(t).search(hay)]
