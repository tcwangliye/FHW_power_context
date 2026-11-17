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

"""Lossless, identity-stable captured evidence for individual CSV records."""

from __future__ import annotations

import csv
import io
import json
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, field_validator, model_validator

from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


def _document_id(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("document_id must be nonempty without surrounding whitespace")  # noqa: TRY003
    return value


def _primary_key(value: str) -> str:
    if not value.strip():
        raise ValueError("primary_key must not be blank")  # noqa: TRY003
    return value


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Encode both identity components independently without trimming them."""

    _document_id(document_id)
    if not primary_key_value.strip():
        raise ValueError("primary_key_value must not be blank")  # noqa: TRY003
    identity = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(identity) > 256:
        raise ValueError("CSV source identity exceeds 256 characters")  # noqa: TRY003
    return identity


def _columns(columns: tuple[str, ...], primary_key: str) -> None:
    if not columns or any(not column.strip() for column in columns):
        raise ValueError("CSV headers must be nonempty")  # noqa: TRY003
    if len(set(columns)) != len(columns):
        raise ValueError("CSV headers must be unique")  # noqa: TRY003
    if primary_key not in columns:
        raise ValueError("primary_key must exactly match a header")  # noqa: TRY003


class CSVDocumentInput(BaseModel):
    """An offline UTF-8 CSV document and its exact primary-key column."""

    document_id: str
    primary_key: str
    content: bytes

    _validate_document_id = field_validator("document_id")(_document_id)
    _validate_primary_key = field_validator("primary_key")(_primary_key)


class CSVRowInput(BaseModel):
    """One complete record, including its original column order and strings."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def validate_record(self) -> CSVRowInput:
        _document_id(self.document_id)
        _primary_key(self.primary_key)
        _columns(self.columns, self.primary_key)
        csv_row_source_id(self.document_id, self.primary_key_value)
        if len(self.columns) != len(self.values):
            raise ValueError("CSV record width does not match the header")  # noqa: TRY003
        if self.values[self.columns.index(self.primary_key)] != self.primary_key_value:
            raise ValueError("primary_key_value does not match the record")  # noqa: TRY003
        return self


class CSVRowSource(Source):
    """Captured row evidence that remains readable without the original file."""

    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED
    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Validate the whole document before returning any record."""

    try:
        text = value.content.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader, None)
        if header is None:
            raise ValueError("CSV document has no header")  # noqa: TRY003
        columns = tuple(header)
        _columns(columns, value.primary_key)
        key_index = columns.index(value.primary_key)
        rows: list[CSVRowInput] = []
        observed: set[str] = set()
        for fields in reader:
            if not fields:
                continue
            if len(fields) != len(columns):
                raise ValueError("CSV record width does not match the header")  # noqa: TRY003
            key = fields[key_index]
            row = CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=key,
                columns=columns,
                values=tuple(fields),
            )
            if key in observed:
                raise ValueError("duplicate CSV primary key")  # noqa: TRY003
            observed.add(key)
            rows.append(row)
        return tuple(rows)
    except (UnicodeError, csv.Error) as error:
        raise ValueError("invalid UTF-8 CSV document") from error  # noqa: TRY003


class _CSVRowAdapter:
    input_class = CSVRowInput
    name = "csv-row"
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        identity = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(name=identity, source_id=identity, **value.model_dump())

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput.model_validate(source.model_dump())


class _CSVRowProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type="csv-row",
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(_CSVRowAdapter(), projections=(_CSVRowProjection(),))

__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]
