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

"""Source availability propagation with durable, retryable native mutations."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field, field_validator

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.source_lifecycle import SourceLifecycleRepository
from powercontext.sources import SourceMaterialization, SourceRef


class SourceLifecycleError(ValueError):
    """Invalid dependency binding, lifecycle event, or generation reference."""


class SourceLifecycleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_id: str
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: bool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("lifecycle text must not be blank")  # noqa: TRY003
        return value


class SourceLifecycleApplication:
    def __init__(self, runtime, database, sources) -> None:
        self._runtime = runtime
        self._database = database
        self._sources = sources
        self._locks: dict[str, asyncio.Lock] = {}

    def for_scope(self, scope_id: str):
        from powercontext.builtin.scope.models import validate_scope_id

        scope_id = validate_scope_id(scope_id)
        return ScopedSourceLifecycle(
            self._runtime, self._database, self._sources, scope_id, self._locks.setdefault(scope_id, asyncio.Lock())
        )


class ScopedSourceLifecycle:
    def __init__(self, runtime, database, sources, scope_id, lock) -> None:
        self._runtime, self._database, self._scope_id, self._lock = runtime, database, scope_id, lock
        self._sources = sources
        self._repository = SourceLifecycleRepository()

    async def _load(self) -> dict:
        async with self._database.transaction() as connection:
            return await self._repository.load(connection, self._scope_id)

    async def _save(self, nodes: dict) -> None:
        async with self._database.transaction() as connection:
            await self._repository.save(connection, self._scope_id, nodes)

    async def register_source(self, node_id: str, reference: SourceRef, *, revision: int) -> None:
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("source revision must be a positive integer")  # noqa: TRY003
        try:
            async with self._database.transaction() as connection:
                stored = await self._sources.get(connection, self._scope_id, reference)
        except Exception as error:
            raise SourceLifecycleError("source binding does not exist") from error  # noqa: TRY003
        if stored.value.materialization is not SourceMaterialization.CAPTURED:
            raise SourceLifecycleError("a lifecycle source must be captured")  # noqa: TRY003
        await self._register(
            node_id, {"kind": "source", "reference": reference.model_dump(mode="json"), "revision": revision}
        )

    async def register_memory(
        self, node_id: str, citation: MemoryCitation, *, parents: tuple[str, ...], policy: str
    ) -> None:
        from powercontext.builtin.runtime.models import GetMemoryEntryRequest

        try:
            await self._runtime.memory.for_scope(self._scope_id).get(GetMemoryEntryRequest(citation=citation))
        except Exception as error:
            raise SourceLifecycleError("memory binding does not exist") from error  # noqa: TRY003
        await self._register(
            node_id,
            {
                "kind": "memory",
                "reference": citation.model_dump(mode="json"),
                "parents": list(parents),
                "policy": policy,
            },
        )

    async def register_topic(
        self, node_id: str, reference: ArtifactRef, *, parents: tuple[str, ...], policy: str
    ) -> None:
        if reference.family != "topic-memory":
            raise SourceLifecycleError("topic binding requires a Topic Memory artifact")  # noqa: TRY003
        try:
            await self._runtime.records.for_scope(self._scope_id).get_artifact_revision(
                reference.family, reference.artifact_id, reference.revision
            )
        except Exception as error:
            raise SourceLifecycleError("topic binding does not exist") from error  # noqa: TRY003
        await self._register(
            node_id,
            {
                "kind": "topic",
                "reference": reference.model_dump(mode="json"),
                "parents": list(parents),
                "policy": policy,
            },
        )

    async def _register(self, node_id: str, binding: dict) -> None:
        if not isinstance(node_id, str) or not node_id.strip():
            raise SourceLifecycleError("node_id must not be blank")  # noqa: TRY003
        async with self._lock:
            nodes = await self._load()
            if node_id in nodes:
                if nodes[node_id]["binding"] != binding:
                    raise SourceLifecycleError("node is already bound differently")  # noqa: TRY003
            else:
                if binding["kind"] != "source":
                    parents = binding["parents"]
                    if (
                        not parents
                        or any(not isinstance(parent, str) or not parent.strip() for parent in parents)
                        or not isinstance(binding["policy"], str)
                        or binding["policy"] not in {"all", "any"}
                        or len(set(parents)) != len(parents)
                    ):
                        raise SourceLifecycleError("invalid dependency policy or parents")  # noqa: TRY003
                    if node_id in parents or any(parent not in nodes for parent in parents):
                        raise SourceLifecycleError("parents must already be registered and cannot form a cycle")  # noqa: TRY003
                nodes[node_id] = {"binding": binding, "available": True, "event": None}
            self._propagate(nodes)
            await self._save(nodes)
            await self._synchronize(nodes)

    @staticmethod
    def _propagate(nodes: dict) -> None:
        resolved: dict[str, bool] = {}
        visiting = set()

        def available(node_id):
            if node_id in resolved:
                return resolved[node_id]
            if node_id in visiting:
                raise SourceLifecycleError("cyclic lifecycle graph")  # noqa: TRY003
            visiting.add(node_id)
            node = nodes[node_id]
            binding = node["binding"]
            if binding["kind"] == "source":
                value = True if node["event"] is None else node["event"]["available"]
            else:
                parents = [available(parent) for parent in binding["parents"]]
                value = all(parents) if binding["policy"] == "all" else any(parents)
            resolved[node_id] = value
            node["available"] = value
            visiting.remove(node_id)
            return value

        for node_id in nodes:
            available(node_id)

    async def _synchronize(self, nodes: dict) -> None:
        # Event intent is durable first. A retry rechecks the current native
        # manifest, so partial propagation never creates duplicate revisions.
        async with self._runtime._context(self._scope_id) as context, self._runtime._locked(self._scope_id):
            service = context.artifacts.memory
            memories = [node for node in nodes.values() if node["binding"]["kind"] == "memory"]
            for node in memories:
                citation = MemoryCitation.model_validate(node["binding"]["reference"])
                current = await service.head(citation.memory_ref.artifact_id)
                pointer = next(
                    (entry for entry in current.content.manifest.entries if entry.entry_id == citation.entry_id), None
                )
                if pointer is None or pointer.entry_version_id != citation.entry_version_id:
                    raise SourceLifecycleError("a bound memory version was changed outside the lifecycle")  # noqa: TRY003
                wanted = "active" if node["available"] else "inactive"
                if pointer.state == wanted:
                    continue
                entry = next(entry for entry in await service.entries(current) if entry.entry_id == citation.entry_id)
                if node["available"]:
                    await service.reactivate(current, entries=(entry,))
                else:
                    await service.forget(current, entries=(entry,))

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        async with self._lock:
            nodes = await self._load()
            node = nodes.get(event.node_id)
            if node is None or node["binding"]["kind"] != "source" or node["binding"]["revision"] != event.revision:
                raise SourceLifecycleError("unknown source or lifecycle revision")  # noqa: TRY003
            previous = node["event"]
            value = event.model_dump(mode="json")
            if previous is not None and (
                event.sequence < previous["sequence"] or (event.sequence == previous["sequence"] and value != previous)
            ):
                raise SourceLifecycleError("obsolete or conflicting lifecycle event")  # noqa: TRY003
            node["event"] = value
            self._propagate(nodes)
            await self._save(nodes)
            await self._synchronize(nodes)
            return sorted(key for key, value in nodes.items() if value["available"])

    async def resolve(self, node_id: str, *, purpose: str):
        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError("unknown reference purpose")  # noqa: TRY003
        async with self._lock:
            nodes = await self._load()
            node = nodes.get(node_id)
            if node is None or (purpose == "generation" and not node["available"]):
                raise SourceLifecycleError("node is not available for this purpose")  # noqa: TRY003
            kind = node["binding"]["kind"]
            model = SourceRef if kind == "source" else MemoryCitation if kind == "memory" else ArtifactRef
            return model.model_validate(node["binding"]["reference"])


__all__ = ["SourceLifecycleError", "SourceLifecycleEvent"]
