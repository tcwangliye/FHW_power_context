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

"""Capture the visible original events on a Pi session branch."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, JsonValue

from powercontext.sources import AdapterSourceDefinition
from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


class PiSessionInput(BaseModel):
    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identity(session_id: str, event_id: str) -> str:
    return "pi:" + hashlib.sha256(_canonical([session_id, event_id]).encode()).hexdigest()


def _blocks(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    pieces: list[str] = []
    for block in value:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            pieces.append(block["text"])
        elif block.get("type") == "toolCall":
            arguments = _canonical(block.get("arguments", {}))
            pieces.append(f"[tool_call {block.get('name', '')} {block.get('id', '')}] {arguments}")
    return "\n".join(pieces)


def _visible(session_id: str, record: dict) -> PiEventInput | None:
    event_type = record.get("type")
    role, body = "", ""
    if event_type == "message" and isinstance(record.get("message"), dict):
        message = record["message"]
        role = message.get("role", "")
        if not isinstance(role, str) or not role.strip():
            return None
        body = _blocks(message.get("content"))
        if role == "toolResult" and body:
            body = f"[tool_result {message.get('toolName', '')} {message.get('toolCallId', '')}] {body}"
        elif role == "bashExecution":
            command, output = message.get("command", ""), message.get("output", "")
            if not isinstance(command, str) or not isinstance(output, str):
                raise ValueError("invalid bash execution")  # noqa: TRY003
            body = f"$ {command}\n{output}" if command or output else ""
    elif event_type in {"compaction", "branch_summary"}:
        role, body = event_type, record.get("summary", "")
    elif event_type == "custom_message":
        role, body = "custom", _blocks(record.get("content"))
    if not isinstance(body, str) or not body.strip():
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=record["id"],
        parent_id=record["parentId"],
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _archive(value: PiSessionInput) -> tuple[str, dict[str, dict], tuple[PiEventInput, ...]]:
    try:
        records = [json.loads(line) for line in value.content.decode("utf-8-sig").splitlines() if line.strip()]
    except (UnicodeError, ValueError) as error:
        raise ValueError("invalid Pi JSONL") from error  # noqa: TRY003
    if not records or not isinstance(records[0], dict):
        raise ValueError("missing Pi session header")  # noqa: TRY003
    header = records[0]
    session_id = header.get("id")
    if header.get("type") != "session" or type(header.get("version")) is not int or header["version"] != 3:
        raise ValueError("expected a Pi v3 session")  # noqa: TRY003
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("invalid Pi session identity")  # noqa: TRY003
    nodes = _nodes(records[1:])
    leaf = value.leaf_id if value.leaf_id is not None else next(reversed(nodes), None)
    if leaf is not None and leaf not in nodes:
        raise ValueError("Pi leaf does not exist")  # noqa: TRY003
    chain: list[dict] = []
    while leaf is not None:
        record = nodes[leaf]
        chain.append(record)
        leaf = record["parentId"]
    events = tuple(event for record in reversed(chain) if (event := _visible(session_id, record)) is not None)
    return session_id, nodes, events


def _nodes(records: list) -> dict[str, dict]:
    nodes: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Pi records must be objects")  # noqa: TRY003, TRY004
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id.strip() or event_id in nodes:
            raise ValueError("invalid or duplicate Pi event identity")  # noqa: TRY003
        parent = record.get("parentId")
        if "parentId" not in record or (parent is not None and (not isinstance(parent, str) or parent not in nodes)):
            raise ValueError("Pi parent must reference an earlier event")  # noqa: TRY003
        if not isinstance(record.get("type"), str) or not record["type"].strip():
            raise ValueError("Pi event type must be nonempty text")  # noqa: TRY003
        _canonical(record)
        nodes[event_id] = record
    return nodes


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    return _archive(value)[2]


class _PiEventAdapter:
    input_class = PiEventInput
    name = "pi-event"
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(
            name=_identity(value.session_id, value.event_id),
            materialization=SourceMaterialization.CAPTURED,
            **value.model_dump(),
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput.model_validate(source.model_dump())


class _PiProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pi-event",
            source_id=source.name,
            content=source.content,
            metadata={
                key: getattr(source, key) for key in ("session_id", "event_id", "parent_id", "event_type", "role")
            },
        )


PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(_PiEventAdapter(), projections=(_PiProjection(),))


class PiSessionConnector:
    name = "pi-session-history"
    version = "1"
    source_definitions = frozenset({"pi-event"})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        raw = await asyncio.to_thread(self._path.read_bytes)
        session_id, nodes, events = _archive(PiSessionInput(content=raw, leaf_id=self._leaf_id))
        checkpoint = session.checkpoint
        if checkpoint is None:
            imported: dict[str, str] = {}
        elif (
            isinstance(checkpoint, dict)
            and checkpoint.get("session_id") == session_id
            and isinstance(checkpoint.get("events"), dict)
        ):
            imported = dict(checkpoint["events"])
        else:
            raise ValueError("Pi checkpoint belongs to a different session")  # noqa: TRY003
        for event_id, digest in imported.items():
            if event_id not in nodes or hashlib.sha256(_canonical(nodes[event_id]).encode()).hexdigest() != digest:
                raise ValueError("a previously captured Pi event was changed or removed")  # noqa: TRY003
        for event in events:
            if event.event_id in imported:
                continue
            result = await session.submit(_identity(session_id, event.event_id), "pi-event", event)
            if result.status != "accepted":
                return ConnectorRunCompletion(status=ConnectorRunStatus.INCOMPLETE, checkpoint=checkpoint)
            imported[event.event_id] = hashlib.sha256(_canonical(event.record).encode()).hexdigest()
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"session_id": session_id, "events": imported},
        )


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
]
