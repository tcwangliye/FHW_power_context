# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0

"""Retrieve actual stored headings and continuations with exact page citations."""

import asyncio

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.pdf_pages import PDF_PAGE_SOURCE_DEFINITION, PDFPageInput, PDFPageSource, search_pdf_pages
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


class _PagePipeline:
    async def extract(self, request, /):
        return tuple(
            MemoryEntryInput(
                kind="document", text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"], sources=(source,)
            )
            for source in request.sources
        )


class _RepeatedPagePipeline(_PagePipeline):
    async def extract(self, request, /):
        entries = await super().extract(request)
        return (*entries, *(entry.model_copy(update={"kind": "page-copy"}) for entry in entries))


def test_financial_heading_continuation_and_original_fts_word_forms():
    async def scenario():
        bodies = (
            "Preamble about company policies. " * 35
            + "\nConsolidated Statements of Cash Flows\nOperating activities 18.25",
            "Investing activities\nCapital investment 12.75\nFinancing activities 9.25",
            "Explanatory notes mentioning the statement of cash flows in ordinary prose. " * 15,
            "Restructuring costs total 6.25 in the current period.",
        )
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        sources = [
            await registry.resolve(
                PDFPageInput(
                    document_id="NORTHCORP_2025",
                    document_sha256="a" * 64,
                    page_number=page,
                    text=body,
                )
            )
            for page, body in enumerate(bodies, 1)
        ]
        page_by_source = {}
        for source in sources:
            assert isinstance(source, PDFPageSource)
            page_by_source[source.name] = source.page_number
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_RepeatedPagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Report", summary="Stored pages", idempotency_key="pages")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for source in sources:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(
                        scope_id=scope.scope_id,
                        observation=project_source_for_transport(registry, source),
                    )
                )
            memory = runtime.memory.for_scope(scope.scope_id)
            while (await memory.flush()).processed:
                pass

            async def locations(query, limit):
                citations = await search_pdf_pages(memory, query, limit=limit)
                pages = []
                for citation in citations:
                    stored = await memory.get(GetMemoryEntryRequest(citation=citation))
                    pages.extend(page_by_source[source.source_id] for source in stored.entry.sources)
                return pages

            assert await locations("NORTHCORP statement of cash flows", 2) == [1, 2]
            assert await locations("Which page describes restructuring costs?", 1) == [4]
            found = await locations("NORTHCORP statement of cash flows", 10)
            assert len(found) == len(set(found))

    asyncio.run(scenario())


def test_company_identity_and_cashflow_question_in_a_multi_report_collection(tmp_path):
    from powercontext.builtin.persistence.sqlite import SQLiteConfig

    async def scenario():
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'reports.db'}"))
        cases = []
        for index in range(22):
            cases.extend((
                (f"OTHER_{index}_2025_10K", 1, "Consolidated Statements of Cash Flows\nOperating cash flows 70"),
                (f"OTHER_{index}_2025_10K", 2, "Consolidated Balance Sheets\nTotal assets 90"),
                (
                    f"OTHER_{index}_2025_10K",
                    3,
                    "Consolidated Statements of Income\nRevenue 999\nOperating income 990\nNet income 899",
                ),
            ))
        cases.extend((
            ("3M_2025_10K", 1, "Company introduction and ordinary business descriptions."),
            ("3M_2025_10K", 2, "CONSOLIDATED STATEMENTS OF CASH FLOWS (In millions)\nOperating activities 8"),
            ("3M_2025_10K", 3, "Investing activities\nCapital expenditures 4"),
            ("NORTHSTAR_2025_10K", 1, "Northstar Instruments (NSI) manufactures measurement equipment."),
            (
                "NORTHSTAR_2025_10K",
                2,
                "Consolidated Statements of Income (in millions)\nRevenue 120\nOperating income 24\nNet income 18",
            ),
            ("NORTHSTAR_2025_10K", 3, "Notes on accounting policies."),
            ("NORTHSTAR_2025_10K", 4, "Improving operating margin and gross margin are financial goals. " * 40),
        ))
        async with open_builtin_runtime(config, candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Reports", summary="Several companies", idempotency_key="reports")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for document, number, text in cases:
                source = await registry.resolve(
                    PDFPageInput(document_id=document, document_sha256="b" * 64, page_number=number, text=text)
                )
                await runtime.ingestion.submit(
                    SubmitSourceObservation(
                        scope_id=scope.scope_id, observation=project_source_for_transport(registry, source)
                    )
                )
            while (await runtime.memory.for_scope(scope.scope_id).flush()).processed:
                pass
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            citations = await search_pdf_pages(
                memory, "Does 3M have improving free cashflow conversion in FY2025?", limit=2
            )
            entries = [(await memory.get(GetMemoryEntryRequest(citation=citation))).entry for citation in citations]
            assert len(entries) == 2
            assert all(entry.text.startswith("3M_2025_10K\n") for entry in entries)
            assert "Operating activities 8" in entries[0].text
            assert "Capital expenditures 4" in entries[1].text
            citations = await search_pdf_pages(memory, "Does NSI have improving gross margin in FY25?", limit=2)
            entries = [(await memory.get(GetMemoryEntryRequest(citation=citation))).entry for citation in citations]
            assert len(entries) == 2
            assert all(entry.text.startswith("NORTHSTAR_2025_10K\n") for entry in entries)
            assert "Operating income 24" in entries[0].text

    asyncio.run(scenario())
