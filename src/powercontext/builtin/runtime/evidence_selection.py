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
import re
from collections import Counter
from itertools import pairwise, takewhile

from powercontext.artifacts import ArtifactAddress
from powercontext.builtin.evidence.lexical import lexical_terms
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.evidence_ranker import SentenceEvidenceRanker
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
    normalized = [_SYNONYMS.get(term, term) for term in (*words, *entities) if term not in _QUERY_NOISE]
    return [
        part
        for term in normalized
        for part in (
            [term[index : index + 2] for index in range(len(term) - 1)]
            if len(term) > 2 and re.fullmatch(r"[\u3400-\u9fff]+", term)
            else [term]
        )
    ]


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
        TRUST_POLICY + "\n",
        "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1",
        json
        .dumps({"trust": "untrusted_history", "items": items}, ensure_ascii=False, separators=(",", ":"))
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029"),
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
        *(
            match.end()
            for match in re.finditer(
                r"(?<=[.!?])\s+(?=[A-Z0-9\"(\u3400-\u9fff])|(?<=[\u3002\uff01\uff1f])\s*|\n+", text
            )
        ),
        len(text),
    ]
    return [(start, end) for start, end in pairwise(boundaries) if text[start:end].strip()]


def _query_windows(text: str, query: set[str]):
    words = list(re.finditer(r"\S+", text))
    for index, word in enumerate(words):
        if not query.intersection(_terms(word.group())):
            continue
        for radius in (8, 16, 32):
            yield words[max(0, index - radius)].start(), words[min(len(words) - 1, index + radius)].end()
    for term in sorted(query):
        if not re.search(r"[\u3400-\u9fff]", term):
            continue
        for match in re.finditer(re.escape(term), text):
            for radius in (16, 32, 64):
                yield max(0, match.start() - radius), min(len(text), match.end() + radius)


def _frontier(states):
    highest = -1.0
    result = {}
    for size, (score, fragments) in sorted(states.items()):
        if score > highest + 1e-10:
            result[size] = (score, fragments)
            highest = score
    return result


def _entry_options(address, text, spans, ranker, document_index, capacity, overhead, query):
    repeats = Counter(" ".join(text[start:end].casefold().split()) for start, end in spans)
    cumulative = [0.0]
    for index, (start, end) in enumerate(spans):
        score = ranker.score(document_index, index, start, end)
        cumulative.append(cumulative[-1] + score / repeats[" ".join(text[start:end].casefold().split())])
    endpoints = range(len(spans))
    if len(spans) > 128:
        ranked = sorted(endpoints, key=lambda index: (cumulative[index] - cumulative[index + 1], index))
        endpoints = sorted({0, len(spans) - 1, *ranked[:128]})
    options = {}
    for position, index in enumerate(endpoints):
        start, sentence_end = spans[index]
        for last in endpoints[position:]:
            stop = last + 1
            end = spans[last][1]
            fragment = PreparedEvidenceFragment(address, start, end, text[start:end])
            size = len(_render((fragment,)).encode("utf-8")) - overhead + 1
            if size > capacity:
                break
            options[start, end] = (size, cumulative[stop] - cumulative[index], fragment)
        if (start, sentence_end) in options:
            continue
        for local_start, local_end in _query_windows(text[start:sentence_end], query):
            left, right = start + local_start, start + local_end
            fragment = PreparedEvidenceFragment(address, left, right, text[left:right])
            size = len(_render((fragment,)).encode("utf-8")) - overhead + 1
            if size <= capacity:
                options[left, right] = (size, ranker.score(document_index, index, left, right), fragment)
    return options


def _entry_plans(text, options, capacity):
    positions = sorted({0, len(text), *(position for bounds in options for position in bounds)})
    indices = {position: index for index, position in enumerate(positions)}
    choices = [[] for _ in positions]
    for (start, end), (size, score, fragment) in options.items():
        choices[indices[start]].append((indices[end], size, score, fragment))
    states = [{} for _ in positions]
    states[0][0] = (0.0, ())
    for index, candidates in enumerate(choices[:-1]):
        for size, (score, fragments) in _frontier(states[index]).items():
            if score > states[index + 1].get(size, (-1, ()))[0]:
                states[index + 1][size] = (score, fragments)
            for stop, added_size, added_score, fragment in candidates:
                total_size = size + added_size
                if total_size <= capacity and score + added_score > states[stop].get(total_size, (-1, ()))[0]:
                    states[stop][total_size] = (score + added_score, (*fragments, fragment))
    return _frontier(states[-1])


def _fit_entries(entries, spans, ranker, max_bytes, query):
    overhead = len(_render(()).encode("utf-8"))
    capacity = max_bytes - overhead + 1
    states = {0: (0.0, ())}
    seen_texts = set()
    for index, (address, text) in enumerate(entries):
        if text in seen_texts:
            continue
        seen_texts.add(text)
        options = _entry_options(address, text, spans[index], ranker, index, capacity, overhead, query)
        alternatives = _entry_plans(text, options, capacity)
        combined = {}
        for size, (score, fragments) in states.items():
            for added_size, (added_score, added_fragments) in alternatives.items():
                total_size = size + added_size
                if total_size <= capacity and score + added_score > combined.get(total_size, (-1, ()))[0]:
                    combined[total_size] = (score + added_score, (*fragments, *added_fragments))
        states = _frontier(combined)
    return max(states.values(), key=lambda item: item[0])[1] if states else ()


def select_memory_fragments(request, groups) -> PreparedContextBuild:
    entries = _entries(groups)
    query = set(_terms(request.query))
    aliases = _query_vocabulary(query, entries)

    def terms(text):
        return [aliases.get(term, term) for term in _terms(text)]

    texts = [text for _, text in entries]
    spans = [_sentences(text) for text in texts]
    if query and any(query.intersection(terms(text)) for text in texts):
        ranker = SentenceEvidenceRanker(query, texts, spans, terms)
        selected = _fit_entries(entries, spans, ranker, request.max_bytes, query)
    else:
        selected = ()
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
