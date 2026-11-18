"""FastAPI application: journal coverage snapshots -> access segments,
plus retraction/restoration notices -> 'retracted but still accessible' report."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, List, Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import RedirectResponse

from . import db
from .logic import (
    CoverageRow,
    NoticeRow,
    classify_notices,
    compute_segments,
    packages_serving,
)
from .schemas import (
    AccessResponse,
    ArticleOut,
    ArticleUpsert,
    CoverageSnapshot,
    LicenseUpdate,
    NoticeBasis,
    NoticeCreate,
    NoticeOut,
    PackageCreate,
    PackageOut,
    RetractionEntry,
    RetractionReport,
    SegmentOut,
    normalize_doi,
    normalize_issn,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Library Journal Access API",
    version="1.1.0",
    description=(
        "Stores per-package journal coverage snapshots (ISSN, publication "
        "date range, embargo months) and package license windows, then "
        "answers, for a query date + ISSN, which publication date ranges "
        "are accessible and from which packages.  Articles registered by "
        "DOI plus sourced retraction/restoration notices yield a report "
        "of articles that are retracted but still accessible, so readers "
        "do not mistake accessibility for trustworthiness."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------- helpers

def _package_or_404(conn: sqlite3.Connection, package_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM packages WHERE id = ?", (package_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"package {package_id} not found")
    return row


def _package_out(conn: sqlite3.Connection, row: sqlite3.Row) -> PackageOut:
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM coverage WHERE package_id = ?", (row["id"],)
    ).fetchone()["n"]
    return PackageOut(
        id=row["id"],
        name=row["name"],
        license_start=date.fromisoformat(row["license_start"]),
        license_end=date.fromisoformat(row["license_end"]),
        active=bool(row["active"]),
        coverage_rows=count,
    )


# ---------------------------------------------------------------- routes

@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/packages", response_model=PackageOut, status_code=201)
def create_package(body: PackageCreate) -> PackageOut:
    """Register a package with its license start/end dates."""
    with db.write_tx() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO packages (name, license_start, license_end) VALUES (?, ?, ?)",
                (body.name, body.license_start.isoformat(), body.license_end.isoformat()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"package name {body.name!r} already exists")
        db.bump_version(conn)
        return _package_out(conn, _package_or_404(conn, cur.lastrowid))


@app.get("/packages", response_model=List[PackageOut])
def list_packages() -> List[PackageOut]:
    with db.read_tx() as conn:
        rows = conn.execute("SELECT * FROM packages ORDER BY id").fetchall()
        return [_package_out(conn, r) for r in rows]


@app.get("/packages/{package_id}", response_model=PackageOut)
def get_package(package_id: int) -> PackageOut:
    with db.read_tx() as conn:
        return _package_out(conn, _package_or_404(conn, package_id))


@app.put("/packages/{package_id}/license", response_model=PackageOut)
def update_license(package_id: int, body: LicenseUpdate) -> PackageOut:
    """Replace a package's license window (single committed transaction)."""
    with db.write_tx() as conn:
        _package_or_404(conn, package_id)
        conn.execute(
            "UPDATE packages SET license_start = ?, license_end = ? WHERE id = ?",
            (body.license_start.isoformat(), body.license_end.isoformat(), package_id),
        )
        db.bump_version(conn)
        return _package_out(conn, _package_or_404(conn, package_id))


@app.put("/packages/{package_id}/coverage", response_model=PackageOut)
def replace_coverage(package_id: int, body: CoverageSnapshot) -> PackageOut:
    """Atomically replace the package's full coverage snapshot.

    The old snapshot stays in place until the new one commits; any
    failure rolls back to the previous committed version.
    """
    with db.write_tx() as conn:
        _package_or_404(conn, package_id)
        conn.execute("DELETE FROM coverage WHERE package_id = ?", (package_id,))
        conn.executemany(
            "INSERT INTO coverage (package_id, issn, start_date, end_date, embargo_months)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (package_id, e.issn, e.start_date.isoformat(), e.end_date.isoformat(), e.embargo_months)
                for e in body.entries
            ],
        )
        db.bump_version(conn)
        return _package_out(conn, _package_or_404(conn, package_id))


@app.get("/packages/{package_id}/coverage")
def get_coverage(package_id: int) -> dict:
    with db.read_tx() as conn:
        _package_or_404(conn, package_id)
        rows = conn.execute(
            "SELECT issn, start_date, end_date, embargo_months FROM coverage"
            " WHERE package_id = ? ORDER BY issn, start_date",
            (package_id,),
        ).fetchall()
        return {
            "package_id": package_id,
            "data_version": db.current_version(conn),
            "entries": [dict(r) for r in rows],
        }


@app.post("/packages/{package_id}/deactivate", response_model=PackageOut)
def deactivate_package(package_id: int) -> PackageOut:
    """Stop using a package in answers; its rows stay stored and other
    packages' coverage is untouched."""
    with db.write_tx() as conn:
        _package_or_404(conn, package_id)
        conn.execute("UPDATE packages SET active = 0 WHERE id = ?", (package_id,))
        db.bump_version(conn)
        return _package_out(conn, _package_or_404(conn, package_id))


@app.post("/packages/{package_id}/activate", response_model=PackageOut)
def activate_package(package_id: int) -> PackageOut:
    with db.write_tx() as conn:
        _package_or_404(conn, package_id)
        conn.execute("UPDATE packages SET active = 1 WHERE id = ?", (package_id,))
        db.bump_version(conn)
        return _package_out(conn, _package_or_404(conn, package_id))


@app.get("/access", response_model=AccessResponse)
def access(
    issn: str = Query(..., description="Journal ISSN, e.g. 1234-5678"),
    query_date: date = Query(..., alias="date", description="Query date, YYYY-MM-DD"),
) -> AccessResponse:
    """Accessible publication date segments for an ISSN on a query date.

    Only packages licensed *and* active on the query date contribute;
    each package's embargo moves with the query date.  Segments are
    split wherever the set of source packages changes.
    """
    try:
        norm_issn = normalize_issn(issn)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    with db.read_tx() as conn:
        version = db.current_version(conn)
        rows = conn.execute(
            """
            SELECT p.name AS package, c.start_date, c.end_date, c.embargo_months
            FROM coverage c
            JOIN packages p ON p.id = c.package_id
            WHERE c.issn = ?
              AND p.active = 1
              AND p.license_start <= ?
              AND p.license_end   >= ?
            """,
            (norm_issn, query_date.isoformat(), query_date.isoformat()),
        ).fetchall()

    coverage = [
        CoverageRow(
            package=r["package"],
            start=date.fromisoformat(r["start_date"]),
            end=date.fromisoformat(r["end_date"]),
            embargo_months=r["embargo_months"],
        )
        for r in rows
    ]
    segments = compute_segments(coverage, query_date)
    return AccessResponse(
        issn=norm_issn,
        date=query_date,
        data_version=version,
        segments=[
            SegmentOut(start=s.start, end=s.end, sources=list(s.sources)) for s in segments
        ],
    )


# ---------------------------------------------------------------- articles

def _article_out(row: sqlite3.Row) -> ArticleOut:
    return ArticleOut(
        doi=row["doi"], issn=row["issn"], pub_date=date.fromisoformat(row["pub_date"])
    )


@app.put("/articles", response_model=ArticleOut)
def upsert_article(body: ArticleUpsert, response: Response) -> ArticleOut:
    """Register an article by DOI, or correct its ISSN / publication date.

    Idempotent: re-PUTting identical data changes nothing (and does not
    bump ``data_version``).  Notices already stored for the DOI start
    applying to the article as soon as it exists.
    """
    with db.write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM articles WHERE doi = ?", (body.doi,)
        ).fetchone()
        if (
            row is not None
            and row["issn"] == body.issn
            and row["pub_date"] == body.pub_date.isoformat()
        ):
            return _article_out(row)  # unchanged: no write, no version bump
        if row is None:
            conn.execute(
                "INSERT INTO articles (doi, issn, pub_date) VALUES (?, ?, ?)",
                (body.doi, body.issn, body.pub_date.isoformat()),
            )
            response.status_code = 201
        else:
            conn.execute(
                "UPDATE articles SET issn = ?, pub_date = ? WHERE doi = ?",
                (body.issn, body.pub_date.isoformat(), body.doi),
            )
        db.bump_version(conn)
        return _article_out(
            conn.execute("SELECT * FROM articles WHERE doi = ?", (body.doi,)).fetchone()
        )


@app.get("/articles", response_model=List[ArticleOut])
def list_articles(doi: Optional[str] = Query(default=None)) -> List[ArticleOut]:
    """List registered articles, optionally filtered to one DOI."""
    with db.read_tx() as conn:
        if doi is not None:
            try:
                norm = normalize_doi(doi)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            rows = conn.execute(
                "SELECT * FROM articles WHERE doi = ?", (norm,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM articles ORDER BY doi").fetchall()
        return [_article_out(r) for r in rows]


# ---------------------------------------------------------------- notices

def _notice_out(row: sqlite3.Row, max_revision: int, duplicate: bool = False) -> NoticeOut:
    return NoticeOut(
        id=row["id"],
        doi=row["doi"],
        source=row["source"],
        revision=row["revision"],
        kind=row["kind"],
        note=row["note"],
        duplicate=duplicate,
        superseded=row["revision"] < max_revision,
    )


@app.post("/notices", response_model=NoticeOut)
def submit_notice(body: NoticeCreate, response: Response) -> NoticeOut:
    """Submit a retraction/restoration notice with source and revision.

    * Identical resubmission of ``(doi, source, revision)`` -> 200,
      no state change.
    * Same ``(doi, source, revision)`` with a different conclusion ->
      409, nothing stored.
    * A revision lower than one already stored from this source is
      archived but ``superseded`` — it never overrides the newer
      conclusion.
    * The article need not exist yet; the notice takes effect in the
      report as soon as the article is registered.
    """
    with db.write_tx() as conn:
        existing = conn.execute(
            "SELECT * FROM notices WHERE doi = ? AND source = ? AND revision = ?",
            (body.doi, body.source, body.revision),
        ).fetchone()
        if existing is not None:
            if existing["kind"] != body.kind:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"notice {body.doi} / {body.source} / rev {body.revision} "
                        f"already stored as {existing['kind']!r}; "
                        "same source+revision with different content is rejected"
                    ),
                )
            max_rev = conn.execute(
                "SELECT MAX(revision) AS m FROM notices WHERE doi = ? AND source = ?",
                (body.doi, body.source),
            ).fetchone()["m"]
            response.status_code = 200
            return _notice_out(existing, max_rev, duplicate=True)

        cur = conn.execute(
            "INSERT INTO notices (doi, source, revision, kind, note)"
            " VALUES (?, ?, ?, ?, ?)",
            (body.doi, body.source, body.revision, body.kind, body.note),
        )
        db.bump_version(conn)
        row = conn.execute("SELECT * FROM notices WHERE id = ?", (cur.lastrowid,)).fetchone()
        max_rev = conn.execute(
            "SELECT MAX(revision) AS m FROM notices WHERE doi = ? AND source = ?",
            (body.doi, body.source),
        ).fetchone()["m"]
        response.status_code = 201
        return _notice_out(row, max_rev)


@app.get("/notices", response_model=List[NoticeOut])
def list_notices(doi: Optional[str] = Query(default=None)) -> List[NoticeOut]:
    """Audit trail of stored notices, in arrival order."""
    with db.read_tx() as conn:
        if doi is not None:
            try:
                norm = normalize_doi(doi)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            rows = conn.execute(
                "SELECT * FROM notices WHERE doi = ? ORDER BY id", (norm,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM notices ORDER BY id").fetchall()
        max_revs = {
            (r["doi"], r["source"]): r["m"]
            for r in conn.execute(
                "SELECT doi, source, MAX(revision) AS m FROM notices GROUP BY doi, source"
            ).fetchall()
        }
        return [_notice_out(r, max_revs[(r["doi"], r["source"])]) for r in rows]


# ---------------------------------------------------------------- report

@app.get("/retractions", response_model=RetractionReport)
def retraction_report(
    query_date: date = Query(..., alias="date", description="Query date, YYYY-MM-DD"),
) -> RetractionReport:
    """Articles confirmed-retracted but still accessible on the query date.

    ``confirmed``: every source's latest notice says retraction and some
    licensed, active package serves the article on ``date`` (embargo wall
    moves with the query date).  ``disputed``: accessible articles whose
    sources' latest conclusions contradict each other — pending review.

    Articles, notices, coverage and license windows are all read inside
    ONE transaction, so the report always reflects a single committed
    state; any correction triggers re-determination on the next query.
    """
    with db.read_tx() as conn:
        version = db.current_version(conn)
        articles = conn.execute(
            "SELECT doi, issn, pub_date FROM articles ORDER BY doi"
        ).fetchall()
        notice_rows = conn.execute(
            "SELECT doi, source, revision, kind FROM notices ORDER BY id"
        ).fetchall()
        cov_rows = conn.execute(
            """
            SELECT p.name AS package, c.issn, c.start_date, c.end_date, c.embargo_months
            FROM coverage c
            JOIN packages p ON p.id = c.package_id
            WHERE p.active = 1
              AND p.license_start <= ?
              AND p.license_end   >= ?
            """,
            (query_date.isoformat(), query_date.isoformat()),
        ).fetchall()

    notices_by_doi: dict[str, list[NoticeRow]] = defaultdict(list)
    for n in notice_rows:
        notices_by_doi[n["doi"]].append(NoticeRow(n["source"], n["revision"], n["kind"]))

    coverage_by_issn: dict[str, list[CoverageRow]] = defaultdict(list)
    for r in cov_rows:
        coverage_by_issn[r["issn"]].append(
            CoverageRow(
                package=r["package"],
                start=date.fromisoformat(r["start_date"]),
                end=date.fromisoformat(r["end_date"]),
                embargo_months=r["embargo_months"],
            )
        )

    confirmed: List[RetractionEntry] = []
    disputed: List[RetractionEntry] = []
    for a in articles:
        notices = notices_by_doi.get(a["doi"])
        if not notices:
            continue
        status, basis = classify_notices(notices)
        if status not in ("retracted", "disputed"):
            continue
        pub = date.fromisoformat(a["pub_date"])
        packages = packages_serving(coverage_by_issn.get(a["issn"], []), pub, query_date)
        if not packages:
            continue  # not accessible on the query date -> not on the list
        entry = RetractionEntry(
            doi=a["doi"],
            issn=a["issn"],
            pub_date=pub,
            basis=[
                NoticeBasis(source=b.source, revision=b.revision, kind=b.kind)
                for b in basis
            ],
            packages=packages,
        )
        (confirmed if status == "retracted" else disputed).append(entry)

    return RetractionReport(
        date=query_date, data_version=version, confirmed=confirmed, disputed=disputed
    )
