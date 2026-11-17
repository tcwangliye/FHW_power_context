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

"""Event-loop-local embedding deduplication, shared work and bounded LRU."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict, deque
from dataclasses import dataclass

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel


@dataclass(frozen=True, slots=True)
class _SharedVector:
    vector: EmbeddingVector
    batch: object
    usage: InferenceUsage


def _consume_exception(future: asyncio.Future[_SharedVector]) -> None:
    # A cancelled caller may leave shared work with no remaining reader.
    if not future.cancelled():
        future.exception()


class CoalescingEmbeddingModel:
    """Share pending and executing text requests without sharing cancellation."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size <= 0:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a nonnegative integer")  # noqa: TRY003
        if type(coalesce_ms) not in (int, float):
            raise ValueError("coalesce_ms must be a finite nonnegative number")  # noqa: TRY003
        try:
            delay = float(coalesce_ms)
        except OverflowError as error:
            raise ValueError("coalesce_ms must be finite") from error  # noqa: TRY003
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("coalesce_ms must be a finite nonnegative number")  # noqa: TRY003
        self._model = model
        self.profile = model.profile
        self._max_batch_size = max_batch_size
        self._delay = delay / 1000
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[_SharedVector]] = {}
        self._pending: deque[str] = deque()
        self._dispatch_handle: asyncio.TimerHandle | None = None
        self._workers: set[asyncio.Task[None]] = set()

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        if isinstance(texts, str) or any(not isinstance(text, str) for text in texts):
            raise ValueError("texts must be a sequence of strings")  # noqa: TRY003
        if not texts:
            return EmbeddingResult(vectors=())
        loop = asyncio.get_running_loop()
        positions: list[EmbeddingVector | asyncio.Future[_SharedVector]] = []
        waiting: dict[str, asyncio.Future[_SharedVector]] = {}
        for text in texts:
            if text in self._cache:
                self._cache.move_to_end(text)
                positions.append(self._cache[text])
                continue
            future = self._inflight.get(text)
            if future is None:
                future = loop.create_future()
                future.add_done_callback(_consume_exception)
                self._inflight[text] = future
                self._pending.append(text)
            waiting[text] = future
            positions.append(future)
        if self._pending and self._dispatch_handle is None:
            self._dispatch_handle = loop.call_later(self._delay, self._dispatch)
        if waiting:
            await asyncio.gather(*(asyncio.shield(future) for future in waiting.values()))
        vectors: list[EmbeddingVector] = []
        batches: dict[object, InferenceUsage] = {}
        for position in positions:
            if isinstance(position, asyncio.Future):
                shared = position.result()
                vectors.append(shared.vector)
                batches[shared.batch] = shared.usage
            else:
                vectors.append(position)
        usage = InferenceUsage(
            requests=sum(value.requests for value in batches.values()),
            input_tokens=self._tokens(batches, "input_tokens"),
            output_tokens=self._tokens(batches, "output_tokens"),
        )
        return EmbeddingResult(vectors=tuple(vectors), usage=usage)

    @staticmethod
    def _tokens(batches: dict[object, InferenceUsage], field: str) -> int | None:
        values = [value for usage in batches.values() if (value := getattr(usage, field)) is not None]
        return sum(values) if values else None

    def _dispatch(self) -> None:
        self._dispatch_handle = None
        pending = tuple(self._pending)
        self._pending.clear()
        for offset in range(0, len(pending), self._max_batch_size):
            worker = asyncio.create_task(self._run_batch(pending[offset : offset + self._max_batch_size]))
            self._workers.add(worker)
            worker.add_done_callback(self._workers.discard)

    async def _run_batch(self, texts: tuple[str, ...]) -> None:
        try:
            result = await self._model.embed(texts)
            if len(result.vectors) != len(texts):
                raise ValueError("embedding vector count does not match the text count")  # noqa: TRY003, TRY301 - Broadcast malformed results to every waiting caller.
        except BaseException as error:
            for text in texts:
                future = self._inflight.pop(text)
                if not future.done():
                    future.set_exception(error)
            if not isinstance(error, Exception):
                raise
        else:
            batch = object()
            usage = result.usage.model_copy()
            for text, vector in zip(texts, result.vectors, strict=True):
                if self._cache_size:
                    self._cache[text] = vector
                    self._cache.move_to_end(text)
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
                self._inflight.pop(text).set_result(_SharedVector(vector=vector, batch=batch, usage=usage))


__all__ = ["CoalescingEmbeddingModel"]
