# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Conservative lexical normalization for offline evidence ranking."""

import re

_STOP = frozenset([
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "to",
    "of",
    "in",
    "on",
    "by",
    "for",
    "with",
    "from",
    "as",
    "at",
    "and",
    "or",
    "that",
    "this",
    "these",
    "those",
    "it",
    "its",
    "their",
    "there",
    "which",
    "what",
    "how",
    "does",
    "do",
    "did",
    "has",
    "have",
    "had",
    "can",
    "could",
    "would",
    "should",
    "will",
    "than",
    "then",
    "through",
    "into",
    "during",
    "between",
    "using",
    "used",
    "study",
    "studies",
    "show",
    "shows",
    "result",
    "results",
    "suggest",
    "suggests",
])
_TOKEN = re.compile(r"[a-z]+|\d+|[\u3400-\u9fff]+")


def _measure(word: str) -> int:
    return len(re.findall(r"[aeiouy]+[^aeiouy]+", word))


def _inflection(word: str) -> str:
    if word.endswith("sses"):
        word = word[:-2]
    elif word.endswith("ies"):
        word = word[:-3] + "i"
    elif word.endswith("s") and not word.endswith(("ss", "us")):
        word = word[:-1]
    return word


def _participle(word: str) -> str:
    if word.endswith("eed"):
        return word[:-1] if _measure(word[:-3]) > 0 else word
    for ending in ("ingly", "edly", "ing", "ed"):
        root = word[: -len(ending)]
        if word.endswith(ending) and re.search(r"[aeiouy]", root):
            if root.endswith(("at", "bl", "iz")):
                return root + "e"
            if len(root) > 2 and root[-1] == root[-2] and root[-1] not in "aeiouylsz":
                return root[:-1]
            return root
    return word


def stem_term(word: str) -> str:
    """Normalize English inflection without changing stored evidence text."""
    if not word.isascii() or not word.isalpha() or len(word) < 4:
        return word
    word = _participle(_inflection(word))
    if word.endswith("y") and re.search(r"[aeiou]", word[:-1]):
        word = word[:-1] + "i"
    for ending, replacement in (
        ("ational", "ate"),
        ("tional", "tion"),
        ("enci", "ence"),
        ("anci", "ance"),
        ("izer", "ize"),
        ("ization", "ize"),
        ("ation", "ate"),
        ("ator", "ate"),
        ("alism", "al"),
        ("iveness", "ive"),
        ("fulness", "ful"),
        ("ousness", "ous"),
        ("aliti", "al"),
        ("iviti", "ive"),
        ("biliti", "ble"),
        ("icate", "ic"),
        ("ative", ""),
        ("alize", "al"),
        ("iciti", "ic"),
        ("ical", "ic"),
        ("ness", ""),
    ):
        if word.endswith(ending) and _measure(word[: -len(ending)]) > 0:
            word = word[: -len(ending)] + replacement
            break
    if word.endswith("ion") and word[:-3].endswith(("s", "t")) and _measure(word[:-3]) > 1:
        word = word[:-3]
    if word.endswith("e") and _measure(word[:-1]) > 1:
        word = word[:-1]
    return word


def lexical_terms(text: str) -> list[str]:
    result = []
    for word in _TOKEN.findall(text.casefold()):
        if word in _STOP:
            continue
        result.append(stem_term(word))
    return result
