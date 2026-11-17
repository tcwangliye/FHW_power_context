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

"""Additional public-interface checks for adapter boundaries and shared work."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, SourceDefinitionRegistry, TextEvidence
from powercontext.sources.csv_rows import CSV_ROW_SOURCE_DEFINITION, CSVDocumentInput, parse_csv_document


def test_csv_keeps_spaced_keys_and_quoted_crlf_through_source_projection() -> None:
    async def scenario() -> None:
        content = b' key ,message,number\r\n 1 ,"first\r\nsecond",001\r\n1,other,1\r\n'
        rows = parse_csv_document(CSVDocumentInput(document_id="report:一", primary_key=" key ", content=content))
        assert len(rows) == 2
        assert rows[0].primary_key_value == " 1 "
        assert rows[0].values == (" 1 ", "first\r\nsecond", "001")
        registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
        first, second = await asyncio.gather(*(registry.resolve(row) for row in rows))
        assert first.name != second.name
        assert await registry.read(first) == rows[0]
        projection = TextEvidence.model_validate(registry.project(first, TEXT_EVIDENCE_PROJECTION_KEY))
        assert json.loads(projection.content) == {" key ": " 1 ", "message": "first\r\nsecond", "number": "001"}
        assert projection.metadata["columns"] == [" key ", "message", "number"]

    asyncio.run(scenario())


def test_http_reranker_keeps_global_ties_and_rejects_boolean_scores() -> None:
    async def scenario() -> None:
        hit = MemoryHit(
            memory_ref=ArtifactRef(family="memory", artifact_id="test-memory", revision=1),
            entry_id="entry",
            entry_version_id="version",
            text="evidence",
            score=1.0,
            matched_by=("fts",),
        )
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            ranks = [item["rank"] for item in json.loads(request.content)["candidates"]]
            seen.extend(ranks)
            return httpx.Response(200, json={"scores": [{"rank": rank, "score": 0.5} for rank in reversed(ranks)]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = HTTPMemoryReranker("https://score.invalid", client=client, batch_size=2)
            result = await model.rerank("q", (hit,) * 7, 4)
            assert result.selected_ranks == (1, 2, 3, 4)
            assert result.usage.requests == 4
            assert seen == list(range(1, 8))
        invalid = httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"scores": [{"rank": 1, "score": True}]})
        )
        async with httpx.AsyncClient(transport=invalid) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://score.invalid", client=client).rerank("q", (hit,), 1)

    asyncio.run(scenario())


class _MeteredEmbedding:
    profile = EmbeddingProfile(profile_id="adapter-edge-checks", model="deterministic", dimension=1)

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        self.calls.append(texts)
        return EmbeddingResult(
            vectors=tuple((float(len(text)),) for text in texts),
            usage=InferenceUsage(requests=2, input_tokens=len(texts) * 3, output_tokens=0),
        )


def test_embedding_failure_is_local_and_shared_batch_usage_is_charged_once() -> None:
    class Flaky(_MeteredEmbedding):
        failed = False

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            if texts == ("retry",) and not self.failed:
                self.failed = True
                raise RuntimeError("temporary provider failure")  # noqa: TRY003
            return await super().embed(texts)

    async def scenario() -> None:
        base = Flaky()
        model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0, cache_size=4)
        bad, good = await asyncio.gather(model.embed(("retry",)), model.embed(("okay",)), return_exceptions=True)
        assert isinstance(bad, RuntimeError)
        assert isinstance(good, EmbeddingResult)
        assert good.vectors == ((4.0,),)
        retried = await model.embed(("retry", "new", "retry", "okay"))
        assert retried.vectors == ((5.0,), (3.0,), (5.0,), (4.0,))
        assert retried.usage == InferenceUsage(requests=4, input_tokens=6, output_tokens=0)
        cached = await model.embed(("new", "retry", "okay"))
        assert cached.usage.requests == 0

    asyncio.run(scenario())


def test_late_embedding_waiter_survives_cancellation_with_cache_disabled() -> None:
    class Blocking(_MeteredEmbedding):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.started.set()
            await self.release.wait()
            return await super().embed(texts)

    async def scenario() -> None:
        base = Blocking()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=0)
        early = asyncio.create_task(model.embed(("shared",)))
        await base.started.wait()
        late = asyncio.create_task(model.embed(("shared", "shared")))
        await asyncio.sleep(0)
        early.cancel()
        with pytest.raises(asyncio.CancelledError):
            await early
        base.release.set()
        result = await asyncio.wait_for(late, 2)
        assert result.vectors == ((6.0,), (6.0,))
        assert result.usage == InferenceUsage(requests=2, input_tokens=3, output_tokens=0)
        assert base.calls == [("shared",)]
        await model.embed(("shared",))
        assert base.calls == [("shared",), ("shared",)]

    asyncio.run(scenario())
