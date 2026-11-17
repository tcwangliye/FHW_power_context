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

"""Durable scope-local dependency bindings and source event intentions."""

from __future__ import annotations

import json

from sqlalchemy import insert, select, update

from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_GRAPHS_TABLE


class SourceLifecycleRepository:
    async def load(self, connection, scope_id: str) -> dict:
        table = SOURCE_LIFECYCLE_GRAPHS_TABLE
        payload = await connection.scalar(select(table.c.payload).where(table.c.scope_id == scope_id))
        return {} if payload is None else json.loads(bytes(payload))

    async def save(self, connection, scope_id: str, nodes: dict) -> None:
        table = SOURCE_LIFECYCLE_GRAPHS_TABLE
        payload = json.dumps(nodes, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        exists = await connection.scalar(select(table.c.scope_id).where(table.c.scope_id == scope_id))
        if exists is None:
            await connection.execute(insert(table).values(scope_id=scope_id, payload=payload))
        else:
            await connection.execute(update(table).where(table.c.scope_id == scope_id).values(payload=payload))

    async def unavailable_topics(self, connection, scope_id: str) -> frozenset[str]:
        nodes = await self.load(connection, scope_id)
        return frozenset(
            node["binding"]["reference"]["artifact_id"]
            for node in nodes.values()
            if node["binding"]["kind"] == "topic" and not node["available"]
        )
