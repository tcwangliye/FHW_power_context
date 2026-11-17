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


def lexical_terms(text: str) -> list[str]:
    result = []
    for word in _TOKEN.findall(text.casefold()):
        if word in _STOP:
            continue
        if len(word) > 5 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 5 and word.endswith("ing"):
            word = word[:-3]
        elif len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        result.append(word)
    return result
