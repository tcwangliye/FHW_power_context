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

"""Strict HTTP scoring with global, identity-preserving Memory selection."""

from __future__ import annotations

import math

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference.models import InferenceUsage


class HTTPMemoryReranker:
    """Score bounded batches while retaining ranks from the complete pool."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(self, query: str, candidates: tuple[MemoryHit, ...], limit: int, /) -> MemoryRerankDecision:
        if not candidates or type(limit) is not int or not 1 <= limit <= len(candidates):
            raise ValueError("limit must select between one and all candidates")  # noqa: TRY003
        scores: dict[int, int | float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = [
                {"rank": index + 1, "text": candidates[index].text}
                for index in range(start, min(start + self._batch_size, len(candidates)))
            ]
            response = await self._client.post(self._endpoint, json={"query": query, "candidates": batch})
            requests += 1
            response.raise_for_status()
            payload = response.json()
            expected = {item["rank"] for item in batch}
            if not isinstance(payload, dict) or set(payload) != {"scores"} or not isinstance(payload["scores"], list):
                raise ValueError("invalid HTTP reranking response")  # noqa: TRY003
            observed: set[int] = set()
            for item in payload["scores"]:
                if not isinstance(item, dict) or set(item) != {"rank", "score"}:
                    raise ValueError("invalid score record")  # noqa: TRY003
                rank, score = item["rank"], item["score"]
                if type(rank) is not int or rank not in expected or rank in observed:
                    raise ValueError("scores must cover each batch rank exactly once")  # noqa: TRY003
                if type(score) not in (int, float) or (isinstance(score, float) and not math.isfinite(score)):
                    raise ValueError("score must be a finite number")  # noqa: TRY003
                observed.add(rank)
                scores[rank] = score
            if observed != expected:
                raise ValueError("response omitted a batch rank")  # noqa: TRY003
        selected = tuple(sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit])
        return MemoryRerankDecision(selected_ranks=selected, usage=InferenceUsage(requests=requests))


__all__ = ["HTTPMemoryReranker"]
