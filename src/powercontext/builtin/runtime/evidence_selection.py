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

"""Query-aware literal sentence windows with exact output-byte accounting."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from itertools import pairwise, takewhile

from powercontext.artifacts import ArtifactAddress
from powercontext.builtin.evidence.lexical import lexical_terms
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.models import PreparedContext
from powercontext.builtin.runtime.prepared_context import (
    MemoryEntryAddress,
    PreparedContextBuild,
    PreparedEvidenceFragment,
)
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

_QUERY_NOISE = frozenset(lexical_terms("no not highly key role play plays factor levels known"))
_SYNONYMS = {}
for _group in (
    "mortality death deaths fatality fatalities",
    "medication medications drug drugs pharmacotherapy",
    "effective ineffective effectiveness efficacy efficacious",
    "stable stability stabilize stabilizes stabilization",
    "expression expressed express",
):
    _normalized = lexical_terms(_group)
    _SYNONYMS.update(dict.fromkeys(_normalized, _normalized[0]))


def _terms(text: str) -> list[str]:
    # Keep scientific identifiers intact: ABC1 and XYZ1 must not acquire a
    # spurious similarity just because both contain the digit 1.
    text = re.sub(r"\b([A-Z]{2,})\s+(\d+)\b", r"\1\2", text).casefold()
    identifier = r"\b[a-z]+(?:-?\d+[a-z]*)+\b"
    entities = [word.replace("-", "") for word in re.findall(identifier, text)]
    words = lexical_terms(re.sub(identifier, " ", text))
    return [_SYNONYMS.get(term, term) for term in (*words, *entities) if term not in _QUERY_NOISE]


def _query_vocabulary(query, entries):
    aliases = {}
    for word in {term for _, text in entries for term in _terms(text)} - query:
        if len(word) < 6 or not word.isalpha():
            continue
        related = []
        for term in query:
            prefix = sum(1 for _ in takewhile(lambda pair: pair[0] == pair[1], zip(word, term, strict=False)))
            if term.isalpha() and prefix >= 6 and prefix >= 0.8 * min(len(word), len(term)):
                related.append((prefix, term))
        if related:
            aliases[word] = max(related)[1]
    return aliases


def _render(fragments: tuple[PreparedEvidenceFragment, ...]) -> str:
    items = [
        {
            "citation": {
                "scope_id": fragment.address.memory.scope_id,
                "memory_ref": fragment.address.memory.artifact.model_dump(mode="json"),
                "entry_id": fragment.address.entry_id,
                "entry_version_id": fragment.address.entry_version_id,
            },
            "content": fragment.text,
            "start": fragment.start,
            "end": fragment.end,
        }
        for fragment in fragments
    ]
    return "\n".join((
        TRUST_POLICY,
        "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1",
        json.dumps({"trust": "untrusted_history", "items": items}, ensure_ascii=False, separators=(",", ":")),
        "END_POWERCONTEXT_PREPARED_CONTEXT_V1",
    ))


def _entries(groups):
    entries = []
    seen = []
    for group in groups:
        if group.hits and group.memory_ref is None:
            raise PreparedContextInvariantError("memory-ref-missing")
        for hit in group.hits:
            if hit.memory_ref != group.memory_ref:
                raise PreparedContextInvariantError("memory-ref-mismatch")
            address = MemoryEntryAddress(
                ArtifactAddress(scope_id=group.scope_id, artifact=hit.memory_ref), hit.entry_id, hit.entry_version_id
            )
            if address not in seen:
                entries.append((address, hit.text))
                seen.append(address)
    return entries


def _sentences(text: str):
    # Delimit at sentence boundaries, rather than excluding every punctuation
    # character: decimal points and abbreviations are part of the evidence.
    boundaries = [
        0,
        *(match.end() for match in re.finditer(r"(?<=[.!?])\s+(?=[A-Z0-9\"(\u3400-\u9fff])|\n+", text)),
        len(text),
    ]
    return [(start, end) for start, end in pairwise(boundaries) if text[start:end].strip()]


def _windows(text: str):
    spans = _sentences(text)
    for i in range(len(spans)):
        for width in range(1, 7):
            if i + width <= len(spans):
                yield spans[i][0], spans[i + width - 1][1]
    if spans:
        yield 0, len(text)


def _rhetorical_weight(text: str, start: int, end: int) -> float:
    labels = list(
        re.finditer(
            r"\b(BACKGROUND|IMPORTANCE|OBJECTIVES?|PURPOSE|METHODS?|DESIGN|PARTICIPANTS|RESULTS?|FINDINGS|CONCLUSIONS?|INTERPRETATION)\b",
            text[:end],
        )
    )
    selected = [match.group() for match in labels if start <= match.start() < end]
    if not selected and labels:
        selected = [labels[-1].group()]
    body = text[start:end].casefold()
    numerical = bool(
        re.search(r"\d+(?:[.]\d+)?\s*(?:%|fold|\bvs\b)|\b(?:mean|cost|rate|ratio|risk|compared)\b.*\d", body)
    )
    empirical = bool(
        re.search(
            r"\b(?:found|showed|demonstrated|observed|associated|correlated|increased|decreased|reduced|higher|lower|revealed|resulted|indicated)\b",
            body,
        )
    )
    if any(label.startswith(("RESULT", "FINDING")) for label in selected):
        return 2.0 if numerical or empirical else 1.2
    if any(label.startswith(("CONCLUSION", "INTERPRETATION")) for label in selected):
        return 1.6
    if selected:
        return 0.6
    if empirical:
        return 1.5
    if numerical:
        return 1.4
    if re.search(r"\b(?:objective|hypothes[ei]s|aim|purpose|to (?:evaluate|investigate|examine))\b", body):
        return 0.5
    return 0.85 if start == 0 else 1.0


def _query_windows(text: str, query: set[str]):
    """Fallback for evidence inside a sentence too large for the envelope."""
    words = list(re.finditer(r"\S+", text))
    for index, word in enumerate(words):
        if not query.intersection(_terms(word.group())):
            continue
        for radius in (8, 16, 32):
            yield words[max(0, index - radius)].start(), words[min(len(words) - 1, index + radius)].end()


def _candidates(entries, utility, query, max_bytes):
    candidates: list[tuple[float, PreparedEvidenceFragment]] = []
    for address, text in entries:
        entry_candidates = []
        for start, end in _windows(text):
            score = utility(text, start, end)
            if score > 0:
                entry_candidates.append((score, PreparedEvidenceFragment(address, start, end, text[start:end])))
        if not any(
            query.intersection(_terms(fragment.text)) and len(_render((fragment,)).encode("utf-8")) <= max_bytes
            for _, fragment in entry_candidates
        ):
            for start, end in _query_windows(text, query):
                entry_candidates.append((
                    utility(text, start, end),
                    PreparedEvidenceFragment(address, start, end, text[start:end]),
                ))
        candidates.extend(entry_candidates)
    candidates.sort(key=lambda item: -item[0])
    return candidates


def _fit_fragments(entries, candidates, max_bytes):
    selected: tuple[PreparedEvidenceFragment, ...] = ()
    # Each accepted interval must add new source characters. Merge intervals from
    # the same entry to avoid paying another citation for neighboring sentences.
    for _, candidate in candidates:
        same = [item for item in selected if item.address == candidate.address]
        if any(item.start <= candidate.start and item.end >= candidate.end for item in same):
            continue
        start, end = candidate.start, candidate.end
        overlap = [item for item in same if item.end >= start and item.start <= end]
        if overlap:
            start = min(start, *(item.start for item in overlap))
            end = max(end, *(item.end for item in overlap))
        source_text = next(text for address, text in entries if address == candidate.address)
        merged = PreparedEvidenceFragment(candidate.address, start, end, source_text[start:end])
        trial = (*(item for item in selected if item not in overlap), merged)
        if len(_render(trial).encode("utf-8")) <= max_bytes:
            selected = trial
    return selected


def select_memory_fragments(request, groups) -> PreparedContextBuild:
    entries = _entries(groups)
    query = set(_terms(request.query))
    aliases = _query_vocabulary(query, entries)

    def terms(text):
        return [aliases.get(term, term) for term in _terms(text)]

    frequency = Counter(term for _, text in entries for term in set(terms(text)))
    weights = {
        term: math.log(1 + (len(entries) - frequency[term] + 0.5) / (frequency[term] + 0.5))
        * (0.25 if term.isdigit() else 3 if any(char.isdigit() for char in term) else 1)
        for term in query
    }

    document_scores = {}
    for _, text in entries:
        counts = Counter(terms(text))
        title = set(terms(text[:200].split(". ")[0]))
        document_scores[text] = sum(
            weights[term] * (counts[term] * 2 / (counts[term] + 1) + (term in title)) for term in query if counts[term]
        ) / (1 + sum(counts.values()) / 1000)
    strongest = max(document_scores.values(), default=1) or 1

    def sentence_score(text: str, start: int, end: int) -> float:
        tokens = terms(text[start:end])
        counts = Counter(tokens)
        local = sum(weights[term] * (1 + math.log(counts[term])) for term in query if counts[term])
        document = document_scores[text]
        score = (local + 0.3 * document) * (document / strongest) ** 3
        return score * _rhetorical_weight(text, start, end) / (1 + len(tokens) / 50)

    sentence_scores = {
        text: [(start, end, sentence_score(text, start, end)) for start, end in _sentences(text)] for _, text in entries
    }

    def utility(text: str, start: int, end: int) -> float:
        scores = {}
        for a, b, score in sentence_scores[text]:
            if a >= start and b <= end:
                key = " ".join(text[a:b].casefold().split())
                scores[key] = max(scores.get(key, 0), score)
        if not scores:
            return sentence_score(text, start, end)
        # Add evidence across sentences. A window spanning a section boundary
        # must not lend its RESULTS label to background or objective sentences.
        # Repeating the same sentence does not add another piece of evidence.
        return sum(scores.values()) / (1 + len(text[start:end].encode("utf-8")) / 1500)

    candidates = _candidates(entries, utility, query, request.max_bytes)
    selected = _fit_fragments(entries, candidates, request.max_bytes)
    if not selected:
        return PreparedContextBuild(
            context=PreparedContext(status="empty", content=None, content_bytes=0), origins=(), fragments=()
        )
    content = _render(selected)
    return PreparedContextBuild(
        context=PreparedContext(status="ready", content=content, content_bytes=len(content.encode("utf-8"))),
        origins=tuple(fragment.address for fragment in selected),
        fragments=selected,
    )
