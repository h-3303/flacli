# SPDX-License-Identifier: GPL-3.0-or-later
"""Text normalisation for matching and Soulseek query building."""

import re
import unicodedata

_FEAT = re.compile(r"\s*[\(\[]?\s*\b(feat|ft|featuring|with)\.?\s+[^\)\]]*[\)\]]?", re.IGNORECASE)
_BRACKETS = re.compile(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]")
_DASH_SUFFIX = re.compile(r"\s+-\s+(remaster(ed)?|live|mono|stereo|radio edit|single version|album version|"
                          r"\d{4} remaster(ed)?|bonus track|demo|edit|mix|remix|version)\b.*$", re.IGNORECASE)
_NON_WORD = re.compile(r"[^0-9a-z]+")
_SPACES = re.compile(r"\s+")

STOPWORDS = {"the", "a", "an", "of", "and", "&"}


_TRANSLIT = str.maketrans({"æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "œ": "oe", "Œ": "OE", "ß": "ss", "ð": "d",
                           "Ð": "D", "þ": "th", "Þ": "TH", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D"})


def strip_accents(text: str) -> str:
    text = text.translate(_TRANSLIT)
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize(text: str | None) -> str:
    """Lowercase ASCII words separated by single spaces; '&' becomes 'and'."""
    if not text:
        return ""

    text = strip_accents(text).lower().replace("&", " and ")
    text = _NON_WORD.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def tokens(text: str | None, drop_stopwords=False) -> list[str]:
    words = normalize(text).split()

    if drop_stopwords:
        words = [w for w in words if w not in STOPWORDS] or words

    return words


def clean_title(title: str) -> str:
    """Remove featuring credits, bracketed text and '- Remastered'-style suffixes."""
    cleaned = _FEAT.sub("", title)
    cleaned = _BRACKETS.sub("", cleaned)
    cleaned = _DASH_SUFFIX.sub("", cleaned)
    return _SPACES.sub(" ", cleaned).strip() or title.strip()


def clean_artist(artist: str) -> str:
    """First credited artist, without featuring credits."""
    cleaned = _FEAT.sub("", artist)
    for separator in (";", ",", " / ", " x ", " X ", " & "):
        if separator in cleaned:
            cleaned = cleaned.split(separator, 1)[0]
    return _SPACES.sub(" ", cleaned).strip() or artist.strip()


def query_words(*parts: str) -> str:
    """A Soulseek query: cleaned, accent-free, punctuation-free words. Duplicates removed, order kept."""
    seen = []

    for part in parts:
        for word in tokens(part):
            if word not in seen:
                seen.append(word)

    return " ".join(seen)


def token_overlap(needle: str | None, haystack: str | None) -> float:
    """Fraction of needle tokens present in haystack tokens (0 when needle is empty)."""
    need = tokens(needle, drop_stopwords=True)

    if not need:
        return 0.0

    have = set(tokens(haystack))
    return sum(1 for w in need if w in have) / len(need)
