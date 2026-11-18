"""Pydantic request/response models."""

from __future__ import annotations

import re
from datetime import date
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

_ISSN_RE = re.compile(r"^\d{4}-?\d{3}[\dXx]$")
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def normalize_issn(value: str) -> str:
    """Accept ``12345678`` / ``1234-5678`` / ``1234-567X``; store ``1234-567X``."""
    if not isinstance(value, str):
        raise ValueError("ISSN must be a string")
    v = value.strip().upper()
    if not _ISSN_RE.match(v):
        raise ValueError("invalid ISSN: expected 7 digits + digit/X, e.g. 1234-5678")
    return f"{v[:4]}-{v[-4:]}"


def normalize_doi(value: str) -> str:
    """Normalize a DOI to its bare lowercase form.

    Accepts ``10.1000/XYZ``, ``doi:10.1000/xyz`` and
    ``https://doi.org/10.1000/xyz``; stores ``10.1000/xyz``.
    """
    if not isinstance(value, str):
        raise ValueError("DOI must be a string")
    v = value.strip()
    lowered = v.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if lowered.startswith(prefix):
            v = v[len(prefix):]
            break
    v = v.strip().lower()
    if not _DOI_RE.match(v):
        raise ValueError("invalid DOI: expected form 10.<registrant>/<suffix>")
    return v


class CoverageEntry(BaseModel):
    issn: str
    start_date: date
    end_date: date
    embargo_months: int = Field(default=0, ge=0)

    @field_validator("issn")
    @classmethod
    def _issn(cls, v: str) -> str:
        return normalize_issn(v)

    @model_validator(mode="after")
    def _range(self) -> "CoverageEntry":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class CoverageSnapshot(BaseModel):
    """Full replacement snapshot for one package (empty list clears it)."""

    entries: List[CoverageEntry]


class PackageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    license_start: date
    license_end: date

    @model_validator(mode="after")
    def _range(self) -> "PackageCreate":
        if self.license_end < self.license_start:
            raise ValueError("license_end must be on or after license_start")
        return self


class LicenseUpdate(BaseModel):
    license_start: date
    license_end: date

    @model_validator(mode="after")
    def _range(self) -> "LicenseUpdate":
        if self.license_end < self.license_start:
            raise ValueError("license_end must be on or after license_start")
        return self


class PackageOut(BaseModel):
    id: int
    name: str
    license_start: date
    license_end: date
    active: bool
    coverage_rows: int


class SegmentOut(BaseModel):
    start: date
    end: date
    sources: List[str]


class AccessResponse(BaseModel):
    issn: str
    date: date
    data_version: int
    segments: List[SegmentOut]


# ---------------------------------------------------------------- articles

class ArticleUpsert(BaseModel):
    """Register an article by DOI, or correct its ISSN / publication date."""

    doi: str
    issn: str
    pub_date: date

    @field_validator("doi")
    @classmethod
    def _doi(cls, v: str) -> str:
        return normalize_doi(v)

    @field_validator("issn")
    @classmethod
    def _issn(cls, v: str) -> str:
        return normalize_issn(v)


class ArticleOut(BaseModel):
    doi: str
    issn: str
    pub_date: date


# ---------------------------------------------------------------- notices

class NoticeCreate(BaseModel):
    """A retraction or restoration notice from one source.

    ``(doi, source, revision)`` identifies the notice.  Resubmitting the
    identical notice is an idempotent no-op; the same key with a
    different conclusion is rejected with 409.
    """

    doi: str
    source: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=1)
    kind: Literal["retraction", "restoration"]
    note: Optional[str] = Field(default=None, max_length=500)

    @field_validator("doi")
    @classmethod
    def _doi(cls, v: str) -> str:
        return normalize_doi(v)

    @field_validator("source")
    @classmethod
    def _source(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("source must not be blank")
        return v


class NoticeOut(BaseModel):
    id: int
    doi: str
    source: str
    revision: int
    kind: str
    note: Optional[str] = None
    # True when an identical notice was already stored (no state change).
    duplicate: bool = False
    # True when this source already holds a higher revision for the DOI,
    # so this notice is archived but does not change the conclusion.
    superseded: bool = False


class NoticeBasis(BaseModel):
    source: str
    revision: int
    kind: str


# ---------------------------------------------------------------- report

class RetractionEntry(BaseModel):
    doi: str
    issn: str
    pub_date: date
    basis: List[NoticeBasis]      # every source's latest notice
    packages: List[str]           # packages serving the article on the query date


class RetractionReport(BaseModel):
    date: date
    data_version: int
    # Confirmed-retracted articles still accessible on the query date.
    confirmed: List[RetractionEntry]
    # Accessible articles whose sources' latest conclusions contradict
    # each other (pending manual review).
    disputed: List[RetractionEntry]
