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
    output_class: type[BaseModel] = TextEvidence

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


_QUESTION_NOISE = frozenset([
    "answer",
    "respond",
    "response",
    "question",
    "define",
    "assuming",
    "perspective",
    "investment",
    "analyst",
    "details",
    "shown",
    "within",
    "give",
    "relying",
    "calculate",
    "fiscal",
    "fy",
    "usd",
    "million",
    "millions",
    "directly",
    "explicitly",
    "outlined",
    "state",
    "reported",
    "primarily",
    "important",
    "ongoing",
])
_FINANCIAL_CONCEPTS = (
    (
        frozenset({"geographies", "geography", "geographical", "countries", "locations", "regions", "markets"}),
        ("business", "geographic", "regions", "countries", "operations", "segments"),
        ("business", "company overview", "geographic", "segments", "our operations"),
    ),
    (
        frozenset({"legal", "battles", "litigation", "lawsuits", "lawsuit", "proceedings", "disputes"}),
        ("legal", "litigation", "proceedings", "lawsuits"),
        ("legal proceedings", "litigation"),
    ),
    (
        frozenset({"income", "earnings", "ebitda", "profit"}),
        ("income", "profit", "earnings"),
        (
            "statement of income",
            "statements of income",
            "statement of operations",
            "statements of operations",
            "income statement",
        ),
    ),
    (
        frozenset({"cash", "flows", "flow", "capex", "depreciation", "amortization", "ebitda"}),
        ("cash", "flows", "depreciation", "amortization", "expenditures"),
        ("statement of cash flows", "statements of cash flows", "cash flow statement"),
    ),
    (
        frozenset({"balance", "assets", "liabilities", "equity"}),
        ("assets", "liabilities", "equity"),
        ("balance sheet", "balance sheets", "financial position"),
    ),
)


def _query_plan(question: str):
    import re

    from powercontext.builtin.evidence.lexical import lexical_terms

    words = re.findall(r"[a-z]+|\d+", question.casefold())
    concepts = [concept for concept in _FINANCIAL_CONCEPTS if concept[0].intersection(words)]
    meaningful = list(
        dict.fromkeys(
            word
            for word in words
            if word not in _QUESTION_NOISE and len(word) > 2 and not word.isdigit() and lexical_terms(word)
        )
    )
    # Native FTS receives original word forms; scoring normalization must never
    # turn a query into stems that are absent from the stored index.
    queries = [question]
    queries.extend(" ".join(alias[:2]) for _, alias, _ in concepts)
    queries.extend(alias for _, aliases, headings in concepts for alias in (*aliases, *headings))
    queries.extend(meaningful)
    queries.extend(" ".join(meaningful[offset : offset + 4]) for offset in range(0, len(meaningful), 4))
    terms = set(lexical_terms(" ".join(word for word in words if word not in _QUESTION_NOISE)))
    return tuple(dict.fromkeys(query for query in queries if query)), terms, concepts


def _rank_pages(documents, query_terms, concepts, question: str):
    import re

    from powercontext.builtin.evidence.lexical import lexical_terms

    bodies = [hit.text.partition("\n")[2] or hit.text for hit in documents]
    counts = [Counter(lexical_terms(body)) for body in bodies]
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / max(len(lengths), 1) or 1
    frequencies = Counter(term for count in counts for term in set(count))
    question_words = set(re.findall(r"[a-z]+|\d+", question.casefold()))

    def relevance(index):
        count = counts[index]
        score = 0.0
        for term in query_terms:
            frequency = count[term]
            if not frequency:
                continue
            weight = math.log(1 + (len(counts) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            score += weight * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * lengths[index] / average))
        lines = [" ".join(re.findall(r"[a-z]+", line.casefold())) for line in bodies[index].splitlines()]
        contents_page = (
            sum(bool(re.match(r"^item\s+\d", line.strip(), re.I)) for line in bodies[index].splitlines()) >= 6
        )
        for _, aliases, headings in concepts:
            if not contents_page and any(
                title in line and len(line.split()) <= len(title.split()) + 2 for line in lines for title in headings
            ):
                score += 25.0
            alias_terms = set(lexical_terms(" ".join(aliases)))
            score += min(3.0, sum(0.5 for term in alias_terms if count[term]))
        metadata = documents[index].text.partition("\n")[0]
        metadata_words = set(re.findall(r"[a-z]+|\d+", metadata.casefold()))
        named = {
            word for word in metadata_words if word.isalpha() and len(word) > 2 and word not in {"annual", "report"}
        }
        if named and named.intersection(question_words):
            score *= 1.5
        return score * (0.15 if contents_page else 1.0)

    return sorted(range(len(documents)), key=lambda index: (-relevance(index), index))


def _page_locations(reference):
    result = []
    for source in reference.sources:
        if source.source_type != "pdf-page" or not source.source_id.startswith("pdf:"):
            continue
        prefix, _, page = source.source_id.rpartition(":")
        if page.isdigit():
            result.append((prefix, int(page)))
    return result


def _neighbors(anchors, records, limit):
    by_entry = {record.entry.entry_id: record for record in records.entries}
    by_page = {location: record for record in records.entries for location in _page_locations(record.entry)}
    for radius in (1, 2):
        for citation in anchors[: max(1, limit // 4)]:
            record = by_entry.get(citation.entry_id)
            if record is None:
                continue
            for prefix, number in _page_locations(record.entry):
                for neighbor in (number - radius, number + radius):
                    adjacent = by_page.get((prefix, neighbor))
                    if adjacent is not None:
                        yield adjacent.citation


def _select_pages(anchors, records, limit):
    selected = []
    seen = set()
    primary = min(len(anchors), max(1, limit // 2))
    for citation in (*anchors[:primary], *_neighbors(anchors, records, limit), *anchors[primary:]):
        key = (citation.memory_ref.artifact_id, citation.entry_id, citation.entry_version_id)
        if key not in seen:
            selected.append(citation)
            seen.add(key)
        if len(selected) == limit:
            break
    return tuple(selected)


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    from powercontext.builtin.runtime.models import SearchMemoryRequest

    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")  # noqa: TRY003
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must not be blank")  # noqa: TRY003
    queries, terms, concepts = _query_plan(question)
    candidates = {}
    for query in queries:
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=100))
        for hit in page.hits:
            key = (hit.memory_ref.artifact_id, hit.entry_id, hit.entry_version_id)
            candidates.setdefault(key, hit)
    documents = list(candidates.values())
    order = _rank_pages(documents, terms, concepts, question)
    anchors = [
        MemoryCitation(
            memory_ref=documents[index].memory_ref,
            entry_id=documents[index].entry_id,
            entry_version_id=documents[index].entry_version_id,
        )
        for index in order
    ]
    if not anchors:
        return ()
    # Tables and narrative sections often continue across physical page breaks.
    # Use current stored identities to find neighbors of the strongest matches.
    return _select_pages(anchors, await memory.list(), limit)


__all__ = ["PDF_PAGE_SOURCE_DEFINITION", "PDFDocumentInput", "PDFPageInput", "parse_pdf_document", "search_pdf_pages"]
