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


def _windows(text: str):
    spans = [
        (match.start(), match.end())
        for match in re.finditer(r"[^.!?\n]+(?:[.!?]+(?=\s|$)|\n|$)", text)
        if match.group().strip()
    ]
    for i in range(len(spans)):
        for width in (1, 2, 3):
            if i + width <= len(spans):
                yield spans[i][0], spans[i + width - 1][1]


def _query_windows(text: str, query: set[str]):
    """Fallback for evidence inside a sentence too large for the envelope."""
    words = list(re.finditer(r"\S+", text))
    for index, word in enumerate(words):
        if not query.intersection(lexical_terms(word.group())):
            continue
        for radius in (8, 16, 32):
            yield words[max(0, index - radius)].start(), words[min(len(words) - 1, index + radius)].end()


def _candidates(entries, utility, query, max_bytes):
    candidates: list[tuple[float, PreparedEvidenceFragment]] = []
    for address, text in entries:
        entry_candidates = []
        for start, end in _windows(text):
            score = utility(text[start:end])
            if score > 0:
                entry_candidates.append((score, PreparedEvidenceFragment(address, start, end, text[start:end])))
        if not any(len(_render((fragment,)).encode("utf-8")) <= max_bytes for _, fragment in entry_candidates):
            for start, end in _query_windows(text, query):
                entry_candidates.append((
                    utility(text[start:end]),
                    PreparedEvidenceFragment(address, start, end, text[start:end]),
                ))
        candidates.extend(entry_candidates)
    candidates.sort(
        key=lambda item: (
            -item[0],
            item[1].address.memory.scope_id,
            item[1].address.entry_id,
            item[1].start,
            item[1].end,
        )
    )
    return candidates


def select_memory_fragments(request, groups) -> PreparedContextBuild:
    entries = _entries(groups)
    query = set(lexical_terms(request.query))
    frequency = Counter(term for _, text in entries for term in set(lexical_terms(text)))
    weights = {term: 1 + math.log((len(entries) + 1) / (frequency[term] + 1)) for term in query}

    def utility(text: str) -> float:
        tokens = lexical_terms(text)
        counts = Counter(tokens)
        score = sum(weights[term] * (1 + math.log(counts[term])) for term in query if counts[term])
        # Prefer compact complete evidence, without making long sentences unusable.
        return score / (1 + len(tokens) / 100)

    candidates = _candidates(entries, utility, query, request.max_bytes)
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
        if len(_render(trial).encode("utf-8")) <= request.max_bytes:
            selected = trial
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
