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

"""Complete physical PDF pages as immutable captured evidence."""

from __future__ import annotations

import hashlib
import io
import math
from collections import Counter
from itertools import pairwise

from pydantic import BaseModel, Field, field_validator
from pypdf import PdfReader

from powercontext.artifacts import MemoryCitation
from powercontext.sources import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


class PDFDocumentInput(BaseModel):
    document_id: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("document_id must not be blank")  # noqa: TRY003
        return value


class PDFPageInput(BaseModel):
    document_id: str
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: int = Field(strict=True, ge=1)
    text: str

    _validate_document_id = field_validator("document_id")(PDFDocumentInput.validate_document_id.__func__)


class PDFPageSource(Source):
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    try:
        if not value.content or not value.content.lstrip().startswith(b"%PDF-"):
            raise ValueError("input is not a PDF document")  # noqa: TRY003, TRY301
        reader = PdfReader(io.BytesIO(value.content), strict=True)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a password")  # noqa: TRY003, TRY301
        digest = hashlib.sha256(value.content).hexdigest()
        pages = tuple(
            PDFPageInput(
                document_id=value.document_id, document_sha256=digest, page_number=index, text=page.extract_text() or ""
            )
            for index, page in enumerate(reader.pages, start=1)
        )
    except Exception as error:
        raise ValueError("PDF could not be parsed completely") from error  # noqa: TRY003
    return pages


class _PDFPageAdapter:
    input_class = PDFPageInput
    name = "pdf-page"
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        identity = hashlib.sha256(value.document_id.encode()).hexdigest()
        return PDFPageSource(
            name=f"pdf:{identity}:{value.document_sha256}:{value.page_number}",
            materialization=SourceMaterialization.CAPTURED,
            **value.model_dump(),
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput.model_validate(source.model_dump())


class _PDFPageProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(_PDFPageAdapter(), projections=(_PDFPageProjection(),))


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    from powercontext.builtin.evidence.lexical import lexical_terms
    from powercontext.builtin.runtime.models import SearchMemoryRequest

    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")  # noqa: TRY003
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must not be blank")  # noqa: TRY003
    terms = list(dict.fromkeys(lexical_terms(question)))
    candidates = {}
    queries = [question]
    if terms:
        queries.append(" ".join(terms))
        # Independent rare-term searches prevent broad boilerplate from
        # crowding a precise financial heading out of the candidate pool.
        queries.extend(" ".join(terms[offset : offset + 5]) for offset in range(0, len(terms), 5))
    for query in dict.fromkeys(queries):
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=100))
        for hit in page.hits:
            key = (hit.memory_ref.artifact_id, hit.entry_id, hit.entry_version_id)
            candidates.setdefault(key, hit)
    documents = list(candidates.values())
    counts = [Counter(lexical_terms(hit.text)) for hit in documents]
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / max(len(lengths), 1) or 1
    frequencies = Counter(term for count in counts for term in set(count))
    query_set = set(terms)

    def relevance(index):
        count = counts[index]
        score = 0.0
        for term in query_set:
            frequency = count[term]
            if not frequency:
                continue
            weight = math.log(1 + (len(counts) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            score += weight * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * lengths[index] / average))
        # Preserve useful adjacent phrases without requiring an exact question.
        document_terms = lexical_terms(documents[index].text)
        pairs = set(pairwise(document_terms))
        score += sum(0.6 for pair in pairwise(terms) if pair in pairs)
        return score

    order = sorted(range(len(documents)), key=lambda index: (-relevance(index), index))
    return tuple(
        MemoryCitation(
            memory_ref=documents[index].memory_ref,
            entry_id=documents[index].entry_id,
            entry_version_id=documents[index].entry_version_id,
        )
        for index in order[:limit]
    )


__all__ = ["PDF_PAGE_SOURCE_DEFINITION", "PDFDocumentInput", "PDFPageInput", "parse_pdf_document", "search_pdf_pages"]
