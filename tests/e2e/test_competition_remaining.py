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

"""Independent edge and recovery checks for competition product features."""

import asyncio
import io
import json
import random
import sqlite3

import pytest
from pypdf import PdfWriter
from sqlalchemy.exc import IntegrityError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    PrepareContextRequest,
    RememberMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)
from powercontext.builtin.runtime.prepared_context import PreparedContextBuilder, PreparedMemoryCandidates
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document
from powercontext.sources.pi_session import PiSessionInput, parse_pi_session


def test_unicode_chunk_roundtrips_and_delta_edits() -> None:
    rng = random.Random(78)  # noqa: S311 - Deterministic fuzz inputs, not secrets.
    alphabet = ("a", "海", "🙂", "\n", "\t", '"', "\\", "β", "e\u0301")
    for _ in range(40):
        original = "".join(rng.choice(alphabet) for _ in range(90))
        target = original[:23] + "changed 🌊" + original[24:]
        for size in (4, 7, 31, 512):
            base = ContextPack.from_content(original, chunk_bytes=size)
            current = ContextPack.from_content(target, chunk_bytes=size)
            assert all(len(item) <= size for item in base.items)
            delta = ContextPackDelta.between(base, current)
            restored = apply_context_pack_delta(
                ContextPack.from_bytes(base.to_bytes()), ContextPackDelta.from_bytes(delta.to_bytes())
            )
            assert b"".join(restored.items).decode() == target
            assert restored.byte_count == len(restored.to_bytes())
    assert ContextPack.from_content(None).items == ()
    for size in (True, 0, -1, 2.5):
        with pytest.raises(ContextPackError):
            ContextPack.from_content("text", chunk_bytes=size)


def test_pack_rejects_noncanonical_base64_and_forged_byte_counts() -> None:
    pack = ContextPack.build((b"a",))
    wire = json.loads(pack.to_bytes())
    for changed in (
        {**wire, "byte_count": True},
        {**wire, "byte_count": pack.byte_count + 1},
        {**wire, "items": ["YR=="]},
    ):
        with pytest.raises(ContextPackError):
            ContextPack.from_bytes(json.dumps(changed).encode())


def test_pi_large_original_tool_result_and_invalid_parent_type() -> None:
    body = "原始结果🙂" * 12000
    records = [
        {"type": "session", "version": 3, "id": "s"},
        {
            "type": "message",
            "id": "tool",
            "parentId": None,
            "message": {
                "role": "toolResult",
                "toolName": "read",
                "toolCallId": "call",
                "content": [{"type": "text", "text": body}],
            },
        },
    ]
    result = parse_pi_session(
        PiSessionInput(content="\n".join(json.dumps(record, ensure_ascii=False) for record in records).encode())
    )
    assert result[0].content == f"toolResult: [tool_result read call] {body}"
    assert result[0].record == records[1]
    records[1]["parentId"] = []
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content="\n".join(json.dumps(record) for record in records).encode()))


def test_pdf_rejects_nonempty_password_and_retains_blank_pages() -> None:
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=50, height=50)
    stream = io.BytesIO()
    writer.write(stream)
    assert [
        page.text for page in parse_pdf_document(PDFDocumentInput(document_id="all-pages", content=stream.getvalue()))
    ] == ["", "", ""]
    writer.encrypt("required")
    encrypted = io.BytesIO()
    writer.write(encrypted)
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="locked", content=encrypted.getvalue()))


def test_evidence_selection_finds_unpunctuated_tail_and_preserves_scope_identity() -> None:
    text = "Background words without punctuation " * 100 + "cobalt sensor exact calibration value 73"
    reference = ArtifactRef(family="memory", artifact_id="memory", revision=1)
    hit = MemoryHit(
        memory_ref=reference, entry_id="entry", entry_version_id="version", text=text, score=1.0, matched_by=("fts",)
    )
    groups = tuple(
        PreparedMemoryCandidates(scope_id=scope, memory_ref=reference, hits=(hit,)) for scope in ("left", "right")
    )
    request = PrepareContextRequest(query="cobalt sensor calibration", max_bytes=1200, evidence_selection=True)
    result = PreparedContextBuilder().build_scopes_result(
        request=request, current_scope_id="left", memory_candidates=groups
    )
    assert result.fragments
    assert result.context.content_bytes <= 1200
    for fragment, origin in zip(result.fragments, result.origins, strict=True):
        assert fragment.text == text[fragment.start : fragment.end]
        assert fragment.start > 1000
        assert "73" in fragment.text
        assert fragment.address.memory.scope_id in {"left", "right"}
        assert origin == fragment.address


def test_source_lifecycle_retries_native_write_failure_without_duplicate_revisions(tmp_path) -> None:
    async def scenario():
        path = tmp_path / "lifecycle.db"
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{path}"))
        async with open_builtin_runtime(config) as runtime:
            scope = await runtime.scopes.create(
                ScopeDraft(title="Recovery", summary="Native storage interruption", idempotency_key="recovery")
            )
            sid = scope.scope_id
            source = await runtime.sources.for_scope(sid).capture(
                CaptureSource(source_id="source", content="original evidence", metadata={})
            )
            memory = await runtime.memory.for_scope(sid).remember(
                RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text="supported fact"),))
            )
            lifecycle = runtime.lifecycle.for_scope(sid)
            await lifecycle.register_source("source", source.source_ref, revision=1)
            await lifecycle.register_memory("fact", memory.entry.citation, parents=("source",), policy="all")
            event = SourceLifecycleEvent(node_id="source", revision=1, sequence=1, available=False, reason="withdraw")
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TRIGGER fail_memory BEFORE INSERT ON pc_artifacts WHEN NEW.family='memory' BEGIN SELECT RAISE(ABORT, 'interrupted'); END"
                )
            with pytest.raises(IntegrityError):
                await lifecycle.apply(event)
            with sqlite3.connect(path) as connection:
                connection.execute("DROP TRIGGER fail_memory")
        async with open_builtin_runtime(config) as runtime:
            lifecycle = runtime.lifecycle.for_scope(sid)
            assert await lifecycle.apply(event) == []
            current = await runtime.memory.for_scope(sid).list(include_inactive=True)
            assert current.entries[0].state == "inactive"
            head = current.memory_ref
            assert await lifecycle.apply(event) == []
            assert (await runtime.memory.for_scope(sid).list(include_inactive=True)).memory_ref == head
            assert await lifecycle.resolve("fact", purpose="audit") == memory.entry.citation
            restored = event.model_copy(update={"sequence": 2, "available": True})
            assert await lifecycle.apply(restored) == ["fact", "source"]
            assert (await runtime.memory.for_scope(sid).list()).entries[0].state == "active"

    asyncio.run(scenario())


def test_large_pi_capture_survives_restart_and_can_bind_as_lifecycle_source(tmp_path) -> None:
    from powercontext.builtin.runtime import SubmitSourceObservation, open_builtin_contexts
    from powercontext.sources import (
        SourceDefinitionRegistry,
        SourceRef,
        manifest_for_definition,
        project_source_for_transport,
    )
    from powercontext.sources.pi_session import PI_EVENT_SOURCE_DEFINITION

    async def scenario():
        body = "complete original tool output🙂" * 1500
        records = [
            {"type": "session", "version": 3, "id": "large-session"},
            {
                "type": "message",
                "id": "large-event",
                "parentId": None,
                "message": {"role": "toolResult", "toolName": "read", "toolCallId": "call", "content": body},
            },
        ]
        parsed = parse_pi_session(PiSessionInput(content="\n".join(json.dumps(row) for row in records).encode()))[0]
        registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        source = await registry.resolve(parsed)
        observation = project_source_for_transport(registry, source)
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'large.db'}"))
        async with open_builtin_runtime(config) as runtime:
            scope = await runtime.scopes.create(
                ScopeDraft(title="Large capture", summary="Raw evidence", idempotency_key="large")
            )
            await runtime.ingestion.register(manifest_for_definition(PI_EVENT_SOURCE_DEFINITION))
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=observation))
            reference = SourceRef(source_type="pi-event", source_id=source.name)
            await runtime.lifecycle.for_scope(scope.scope_id).register_source("captured-tool", reference, revision=1)
        async with open_builtin_contexts(config) as contexts:
            rows = await (await contexts.get(scope.scope_id)).sources.journal.entries()
            assert len(rows) == 1
            assert rows[0].source.payload["record"] == records[1]
            assert rows[0].source.payload["content"].endswith(body)
        async with open_builtin_runtime(config) as runtime:
            assert (
                await runtime.lifecycle.for_scope(scope.scope_id).resolve("captured-tool", purpose="audit") == reference
            )

    asyncio.run(scenario())
