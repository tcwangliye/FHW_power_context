# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0

"""Evidence ranking regressions through the public prepared-context builder."""

import json

import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryHit
from powercontext.builtin.runtime import PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import (
    MemoryEntryAddress,
    PreparedContextBuilder,
    PreparedMemoryCandidates,
)
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY


def _select(query, documents, max_bytes=1000):
    reference = ArtifactRef(family="memory", artifact_id="memory", revision=3)
    hits = tuple(
        MemoryHit(
            memory_ref=reference,
            entry_id=entry_id,
            entry_version_id=entry_id + "-v1",
            text=text,
            score=1.0,
            matched_by=("fts",),
        )
        for entry_id, text in documents
    )
    result = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query=query, max_bytes=max_bytes, evidence_selection=True),
        current_scope_id="research",
        memory_candidates=(PreparedMemoryCandidates(scope_id="research", memory_ref=reference, hits=hits),),
    )
    assert result.context.content_bytes == len((result.context.content or "").encode()) <= max_bytes
    originals = dict(documents)
    for fragment, origin in zip(result.fragments, result.origins, strict=True):
        assert fragment.text == originals[fragment.address.entry_id][fragment.start : fragment.end]
        assert fragment.address == origin
        assert isinstance(origin, MemoryEntryAddress)
        assert origin.memory.scope_id == "research"
        assert origin.memory.artifact == reference
    return result


def test_decimal_statistics_survive_sentence_selection():
    statistic = "RESULTS The amber catalyst lowered median delay from 3.75 to 1.25 ms (P = 0.004)."
    text = "General archive notes. " * 45 + statistic + " Further trials confirmed the effect."
    result = _select("amber catalyst delay", [("experiment", text)])
    assert any(statistic in fragment.text for fragment in result.fragments)


def test_empirical_results_outweigh_repeated_objectives():
    objective = "OBJECTIVE To assess vaccine effectiveness and infection risk in participants. "
    result_sentence = "RESULTS Vaccinated participants had fewer infections: 4.7% versus 19.8% (risk ratio 0.24)."
    text = objective * 7 + "METHODS Participants were randomly assigned.\n" + result_sentence
    result = _select("vaccine effectiveness infection risk", [("trial", text)], max_bytes=950)
    assert any(result_sentence in fragment.text for fragment in result.fragments)


def test_identifiers_do_not_match_through_an_unrelated_shared_digit():
    irrelevant = "XYZ1 normal expression controls growth. " * 12
    relevant = "ABC1 regulates growth. We observed that ABC1 reduced tumor volume by 42%."
    result = _select("ABC1 normal expression controls growth", [("other", irrelevant), ("target", relevant)])
    assert any(fragment.address.entry_id == "target" and "42%" in fragment.text for fragment in result.fragments)


def test_equal_relevance_preserves_candidate_order_across_opaque_ids():
    text = "The cobalt sensor has an experimentally confirmed threshold of 73 degrees. " * 3
    result = _select("cobalt sensor threshold", [("zzz", text), ("aaa", text)], max_bytes=900)
    assert result.fragments[0].address.entry_id == "zzz"


def test_long_result_sentence_is_considered_even_when_short_introduction_fits():
    text = (
        "The cobalt sensor threshold needs evaluation. "
        + "We measured "
        + "ordinary ambient conditions " * 150
        + "and found the cobalt sensor threshold was 73 degrees in repeated experiments."
    )
    result = _select("cobalt sensor threshold", [("measurement", text)], max_bytes=950)
    assert any("73 degrees" in fragment.text for fragment in result.fragments)


def test_disconnected_results_share_the_budget_without_intervening_background():
    first = "RESULTS The cobalt sensor increased sensitivity by 27% in blinded experiments. "
    last = "The cobalt sensor improved accuracy by 32% in independent experiments."
    background = "Participants followed the standard protocol and completed all required study visits. "
    text = first + background * 10 + last
    result = _select("cobalt sensor sensitivity accuracy", [("trial", text)], max_bytes=1000)
    assert any(first.strip() in fragment.text for fragment in result.fragments)
    assert any(last in fragment.text for fragment in result.fragments)


def test_literal_unicode_separators_do_not_break_the_json_envelope():
    text = "The cobalt sensor\u2028reports a temperature of 73 degrees\u2029under load."
    result = _select("cobalt sensor temperature", [("measurement", text)])
    assert result.context.content is not None
    assert result.context.content.startswith(TRUST_POLICY + "\n\nBEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n")
    payload = json.loads(result.context.content.splitlines()[-2])
    assert payload["items"][0]["content"] == text


def test_unspaced_cjk_query_finds_late_literal_evidence():
    evidence = "关键温度阈值为73摄氏度。"
    text = "这是普通背景资料。" * 100 + evidence
    result = _select("温度阈值", [("measurement", text)], max_bytes=1000)
    assert any(evidence in fragment.text for fragment in result.fragments)


@pytest.mark.parametrize("max_bytes", (1000, 32768))
def test_many_short_sentences_keep_the_late_result(max_bytes):
    evidence = "The cobalt sensor threshold was measured at 73 degrees."
    text = "A. " * 2000 + evidence
    result = _select("cobalt sensor threshold", [("measurement", text)], max_bytes=max_bytes)
    assert any(evidence in fragment.text for fragment in result.fragments)
