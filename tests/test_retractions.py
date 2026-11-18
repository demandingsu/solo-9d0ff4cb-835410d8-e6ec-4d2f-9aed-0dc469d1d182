"""Integration tests for the retraction report: out-of-order notices,
cross-source disputes, corrections, and re-determination triggers."""

from conftest import entry, make_package, put_coverage

ISSN = "1234-5678"
DOI = "10.1234/test.1"
PUB = "2024-06-15"


def put_article(client, doi=DOI, issn=ISSN, pub=PUB):
    return client.put("/articles", json={"doi": doi, "issn": issn, "pub_date": pub})


def post_notice(client, doi=DOI, source="crossref", revision=1, kind="retraction", note=None):
    body = {"doi": doi, "source": source, "revision": revision, "kind": kind}
    if note is not None:
        body["note"] = note
    return client.post("/notices", json=body)


def report(client, date="2026-11-18"):
    r = client.get("/retractions", params={"date": date})
    assert r.status_code == 200, r.text
    return r.json()


def setup_accessible_article(client, doi=DOI, issn=ISSN, pub=PUB, embargo=0):
    """Package + coverage + article so the article is accessible on query dates."""
    pkg = make_package(client, "PackA")
    assert put_coverage(client, pkg, [entry(issn, "2010-01-01", "2030-12-31", embargo)]).status_code == 200
    assert put_article(client, doi, issn, pub).status_code == 201
    return pkg


# ---------------------------------------------------------------- basics

def test_confirmed_retracted_accessible_listed_with_basis_and_packages(client):
    setup_accessible_article(client)
    assert post_notice(client).status_code == 201

    r = report(client)
    assert r["disputed"] == []
    assert len(r["confirmed"]) == 1
    item = r["confirmed"][0]
    assert item["doi"] == DOI
    assert item["issn"] == ISSN
    assert item["pub_date"] == PUB
    assert item["basis"] == [{"source": "crossref", "revision": 1, "kind": "retraction"}]
    assert item["packages"] == ["PackA"]


def test_article_without_notices_not_listed(client):
    setup_accessible_article(client)
    r = report(client)
    assert r["confirmed"] == [] and r["disputed"] == []


def test_restored_article_not_listed(client):
    setup_accessible_article(client)
    post_notice(client, revision=1, kind="retraction")
    post_notice(client, revision=2, kind="restoration")
    r = report(client)
    assert r["confirmed"] == [] and r["disputed"] == []


def test_not_accessible_on_query_date_not_listed(client):
    # license window ends before the query date -> nothing accessible
    pkg = make_package(client, "PackA", lic_start="2020-01-01", lic_end="2025-12-31")
    put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    put_article(client)
    post_notice(client)
    r = report(client, "2026-11-18")
    assert r["confirmed"] == []
    # ...but it was listed while the license still covered the query date
    assert len(report(client, "2025-06-01")["confirmed"]) == 1


# ---------------------------------------------------------------- ordering

def test_out_of_order_lower_revision_does_not_override(client):
    setup_accessible_article(client)
    # newer conclusion arrives first
    r = post_notice(client, revision=2, kind="restoration")
    assert r.status_code == 201 and r.json()["superseded"] is False
    assert report(client)["confirmed"] == []  # restored -> not listed

    # late, lower revision: archived but must not override
    r = post_notice(client, revision=1, kind="retraction")
    assert r.status_code == 201 and r.json()["superseded"] is True
    assert report(client)["confirmed"] == []

    # audit trail keeps both, marking the stale one superseded
    notices = client.get("/notices", params={"doi": DOI}).json()
    assert [(n["revision"], n["kind"], n["superseded"]) for n in notices] == [
        (2, "restoration", False),
        (1, "retraction", True),
    ]


def test_late_retraction_after_restoration_retracts_again(client):
    setup_accessible_article(client)
    post_notice(client, revision=1, kind="retraction")
    post_notice(client, revision=2, kind="restoration")
    assert report(client)["confirmed"] == []
    post_notice(client, revision=3, kind="retraction")
    assert len(report(client)["confirmed"]) == 1


def test_same_revision_different_content_rejected(client):
    setup_accessible_article(client)
    assert post_notice(client, revision=1, kind="retraction").status_code == 201
    before = report(client)

    r = post_notice(client, revision=1, kind="restoration")
    assert r.status_code == 409

    after = report(client)
    assert after["confirmed"] == before["confirmed"]
    assert after["data_version"] == before["data_version"]  # nothing committed


def test_identical_resubmission_is_idempotent(client):
    setup_accessible_article(client)
    assert post_notice(client, note="publisher PDF").status_code == 201
    before = report(client)

    r = post_notice(client, note="publisher PDF")
    assert r.status_code == 200 and r.json()["duplicate"] is True

    after = report(client)
    assert after["data_version"] == before["data_version"]  # no version bump
    assert len(client.get("/notices").json()) == 1          # stored once


# ---------------------------------------------------------------- disputes

def test_cross_source_dispute_goes_to_disputed_list(client):
    setup_accessible_article(client)
    post_notice(client, source="crossref", revision=1, kind="retraction")
    post_notice(client, source="publisher", revision=1, kind="restoration")

    r = report(client)
    assert r["confirmed"] == []
    assert len(r["disputed"]) == 1
    item = r["disputed"][0]
    assert item["doi"] == DOI
    assert item["basis"] == [
        {"source": "crossref", "revision": 1, "kind": "retraction"},
        {"source": "publisher", "revision": 1, "kind": "restoration"},
    ]
    assert item["packages"] == ["PackA"]

    # publisher catches up and agrees -> confirmed
    post_notice(client, source="publisher", revision=2, kind="retraction")
    r = report(client)
    assert r["disputed"] == []
    assert len(r["confirmed"]) == 1
    assert {b["source"] for b in r["confirmed"][0]["basis"]} == {"crossref", "publisher"}


def test_dispute_only_counts_each_sources_latest(client):
    setup_accessible_article(client)
    post_notice(client, source="crossref", revision=1, kind="retraction")
    post_notice(client, source="publisher", revision=1, kind="retraction")
    # publisher's older restoration arrives late: archived, no dispute
    r = post_notice(client, source="publisher", revision=0, kind="restoration")
    assert r.status_code == 422  # revision must be >= 1
    r = post_notice(client, source="publisher", revision=2, kind="restoration")
    assert r.status_code == 201
    assert len(report(client)["disputed"]) == 1
    # late-arriving older retraction from publisher must not revive agreement
    post_notice(client, source="publisher", revision=1, kind="retraction")
    # rev 1 < rev 2 -> superseded; still disputed
    assert len(report(client)["disputed"]) == 1


# ---------------------------------------------------------------- late article

def test_notice_before_article_then_article_enters_determination(client):
    pkg = make_package(client, "PackA")
    put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])

    # notice arrives before the article is known
    assert post_notice(client, doi=DOI, revision=2, kind="retraction").status_code == 201
    r = report(client)
    assert r["confirmed"] == [] and r["disputed"] == []

    # late-registered article must enter the determination
    assert put_article(client, doi=DOI).status_code == 201
    r = report(client)
    assert len(r["confirmed"]) == 1
    assert r["confirmed"][0]["basis"] == [
        {"source": "crossref", "revision": 2, "kind": "retraction"}
    ]


# ---------------------------------------------------------------- corrections

def test_article_correction_rejudges_list(client):
    pkg = make_package(client, "PackA")
    put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    # article registered under an ISSN nothing covers
    assert put_article(client, doi=DOI, issn="9999-0001").status_code == 201
    post_notice(client)
    assert report(client)["confirmed"] == []

    # correcting the ISSN brings it into the determination
    assert put_article(client, doi=DOI, issn=ISSN).status_code == 200
    assert len(report(client)["confirmed"]) == 1

    # correcting pub_date outside the coverage window removes it again
    assert put_article(client, doi=DOI, issn=ISSN, pub="2000-01-01").status_code == 200
    assert report(client)["confirmed"] == []


def test_identical_article_reput_changes_nothing(client):
    setup_accessible_article(client)
    before = report(client)
    r = put_article(client)
    assert r.status_code == 200
    assert report(client)["data_version"] == before["data_version"]


def test_coverage_and_license_changes_rejudge_list(client):
    pkg = setup_accessible_article(client)
    post_notice(client)
    assert len(report(client)["confirmed"]) == 1

    # deactivating the only serving package -> no longer accessible
    assert client.post(f"/packages/{pkg}/deactivate").status_code == 200
    assert report(client)["confirmed"] == []
    assert client.post(f"/packages/{pkg}/activate").status_code == 200
    assert len(report(client)["confirmed"]) == 1

    # shrinking the license window past the query date -> not accessible
    r = client.put(f"/packages/{pkg}/license",
                   json={"license_start": "2020-01-01", "license_end": "2025-12-31"})
    assert r.status_code == 200
    assert report(client, "2026-11-18")["confirmed"] == []

    # shrinking coverage below the pub date -> not accessible
    client.put(f"/packages/{pkg}/license",
               json={"license_start": "2020-01-01", "license_end": "2030-12-31"})
    assert put_coverage(client, pkg, [entry(ISSN, "2010-01-01", "2020-12-31", 0)]).status_code == 200
    assert report(client)["confirmed"] == []


def test_embargo_wall_moves_list_with_query_date(client):
    # 6-month embargo: pub 2024-06-15 is inside the wall on 2024-09-01,
    # accessible once the query date passes 2024-12-15.
    setup_accessible_article(client, embargo=6)
    post_notice(client)
    assert report(client, "2024-09-01")["confirmed"] == []
    assert len(report(client, "2024-12-16")["confirmed"]) == 1
    assert len(report(client, "2026-11-18")["confirmed"]) == 1


def test_multiple_packages_all_listed_as_sources(client):
    setup_accessible_article(client)
    b = make_package(client, "PackB")
    put_coverage(client, b, [entry(ISSN, "2010-01-01", "2030-12-31", 0)])
    post_notice(client)
    assert report(client)["confirmed"][0]["packages"] == ["PackA", "PackB"]


# ---------------------------------------------------------------- consistency

def test_failed_writes_keep_old_results(client):
    setup_accessible_article(client)
    post_notice(client)
    before = report(client)

    # invalid article payloads rejected, nothing committed
    assert put_article(client, doi="not-a-doi").status_code == 422
    assert put_article(client, doi=DOI, issn="junk").status_code == 422
    # invalid notice payloads rejected
    assert post_notice(client, revision=0).status_code == 422
    assert post_notice(client, kind="maybe").status_code == 422
    assert post_notice(client, source="  ").status_code == 422

    after = report(client)
    assert after == before  # identical answer, identical committed version


def test_report_uses_one_committed_version(client):
    setup_accessible_article(client)
    v0 = report(client)["data_version"]
    post_notice(client)
    r = report(client)
    assert r["data_version"] > v0
    # the version in the report matches the version /access reports
    acc = client.get("/access", params={"issn": ISSN, "date": "2026-11-18"}).json()
    assert acc["data_version"] == r["data_version"]


# ---------------------------------------------------------------- normalization

def test_doi_normalization(client):
    setup_accessible_article(client, doi="https://doi.org/10.1234/AbC.X")
    # stored normalized; notice via another spelling lands on the same article
    assert client.get("/articles").json() == [
        {"doi": "10.1234/abc.x", "issn": ISSN, "pub_date": PUB}
    ]
    assert post_notice(client, doi="doi:10.1234/ABC.x").status_code == 201
    r = report(client)
    assert [i["doi"] for i in r["confirmed"]] == ["10.1234/abc.x"]


def test_issn_normalization_on_article(client):
    setup_accessible_article(client, issn="12345678")
    post_notice(client)
    assert report(client)["confirmed"][0]["issn"] == ISSN
