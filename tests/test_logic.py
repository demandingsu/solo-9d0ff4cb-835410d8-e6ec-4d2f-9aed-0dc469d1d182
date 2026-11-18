"""Unit tests for the pure segment/moving-wall logic."""

from datetime import date

from app.logic import (
    CoverageRow,
    NoticeRow,
    classify_notices,
    compute_segments,
    latest_per_source,
    packages_serving,
    subtract_months,
)


def test_subtract_months_plain():
    assert subtract_months(date(2026, 11, 18), 6) == date(2026, 5, 18)
    assert subtract_months(date(2026, 1, 31), 1) == date(2025, 12, 31)
    assert subtract_months(date(2026, 11, 18), 0) == date(2026, 11, 18)


def test_subtract_months_clamps_to_short_month():
    assert subtract_months(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert subtract_months(date(2024, 3, 31), 1) == date(2024, 2, 29)  # leap year


def test_moving_wall_follows_query_date():
    rows = [CoverageRow("A", date(2018, 1, 1), date(2026, 12, 31), 6)]
    seg = compute_segments(rows, date(2026, 11, 18))
    assert seg == [seg[0]]  # single segment
    assert (seg[0].start, seg[0].end) == (date(2018, 1, 1), date(2026, 5, 18))
    # three months later the wall has moved three months too
    seg2 = compute_segments(rows, date(2027, 2, 18))
    assert (seg2[0].start, seg2[0].end) == (date(2018, 1, 1), date(2026, 8, 18))


def test_everything_embargoed_yields_nothing():
    rows = [CoverageRow("A", date(2026, 1, 1), date(2026, 12, 31), 12)]
    assert compute_segments(rows, date(2026, 6, 1)) == []


def test_overlap_splits_at_source_change():
    rows = [
        CoverageRow("A", date(2015, 1, 1), date(2024, 12, 31), 0),
        CoverageRow("B", date(2020, 1, 1), date(2024, 12, 31), 0),
    ]
    seg = compute_segments(rows, date(2026, 1, 1))
    assert [(s.start, s.end, s.sources) for s in seg] == [
        (date(2015, 1, 1), date(2019, 12, 31), ("A",)),
        (date(2020, 1, 1), date(2024, 12, 31), ("A", "B")),
    ]


def test_adjacent_rows_same_package_merge():
    rows = [
        CoverageRow("A", date(2010, 1, 1), date(2010, 12, 31), 0),
        CoverageRow("A", date(2011, 1, 1), date(2011, 12, 31), 0),
    ]
    seg = compute_segments(rows, date(2026, 1, 1))
    assert len(seg) == 1
    assert (seg[0].start, seg[0].end) == (date(2010, 1, 1), date(2011, 12, 31))


def test_different_embargoes_split_sources():
    rows = [
        CoverageRow("A", date(2020, 1, 1), date(2026, 12, 31), 0),
        CoverageRow("B", date(2020, 1, 1), date(2026, 12, 31), 12),
    ]
    seg = compute_segments(rows, date(2026, 6, 15))
    # B's wall cuts at 2025-06-15; A keeps serving up to the query date
    assert [(s.start, s.end, s.sources) for s in seg] == [
        (date(2020, 1, 1), date(2025, 6, 15), ("A", "B")),
        (date(2025, 6, 16), date(2026, 6, 15), ("A",)),
    ]


# ---------------------------------------------------------- notice logic

def test_latest_per_source_ignores_arrival_order():
    notices = [
        NoticeRow("crossref", 3, "retraction"),
        NoticeRow("crossref", 1, "restoration"),  # late, lower revision
        NoticeRow("publisher", 2, "retraction"),
    ]
    best = latest_per_source(notices)
    assert best["crossref"].revision == 3
    assert best["publisher"].revision == 2


def test_classify_no_notices():
    assert classify_notices([]) == ("none", [])


def test_classify_single_source_retraction():
    status, basis = classify_notices([NoticeRow("crossref", 1, "retraction")])
    assert status == "retracted"
    assert basis == [NoticeRow("crossref", 1, "retraction")]


def test_classify_retraction_then_restoration_same_source():
    notices = [
        NoticeRow("crossref", 1, "retraction"),
        NoticeRow("crossref", 2, "restoration"),
    ]
    status, basis = classify_notices(notices)
    assert status == "restored"
    assert basis == [NoticeRow("crossref", 2, "restoration")]


def test_classify_out_of_order_lower_revision_does_not_override():
    # restoration rev 2 arrives first, retraction rev 1 arrives late
    notices = [
        NoticeRow("crossref", 2, "restoration"),
        NoticeRow("crossref", 1, "retraction"),
    ]
    status, _ = classify_notices(notices)
    assert status == "restored"


def test_classify_contradicting_sources_is_disputed():
    notices = [
        NoticeRow("crossref", 2, "retraction"),
        NoticeRow("publisher", 5, "restoration"),
    ]
    status, basis = classify_notices(notices)
    assert status == "disputed"
    # basis carries each source's latest notice, sorted by source
    assert basis == [
        NoticeRow("crossref", 2, "retraction"),
        NoticeRow("publisher", 5, "restoration"),
    ]


def test_classify_dispute_resolves_when_source_advances():
    notices = [
        NoticeRow("crossref", 1, "retraction"),
        NoticeRow("publisher", 1, "restoration"),
        NoticeRow("publisher", 2, "retraction"),
    ]
    status, basis = classify_notices(notices)
    assert status == "retracted"
    assert all(n.kind == "retraction" for n in basis)


def test_packages_serving_respects_moving_wall():
    rows = [CoverageRow("A", date(2020, 1, 1), date(2026, 12, 31), 6)]
    # pub date inside embargo window on 2026-01-01 (wall at 2025-07-01)
    assert packages_serving(rows, date(2025, 8, 1), date(2026, 1, 1)) == []
    # same pub date accessible once the wall has moved past it
    assert packages_serving(rows, date(2025, 8, 1), date(2026, 3, 1)) == ["A"]


def test_packages_serving_merges_multiple_packages():
    rows = [
        CoverageRow("B", date(2020, 1, 1), date(2026, 12, 31), 0),
        CoverageRow("A", date(2020, 1, 1), date(2026, 12, 31), 0),
        CoverageRow("C", date(2024, 1, 1), date(2024, 12, 31), 0),
    ]
    assert packages_serving(rows, date(2024, 6, 1), date(2026, 1, 1)) == ["A", "B", "C"]
    assert packages_serving(rows, date(2025, 6, 1), date(2026, 1, 1)) == ["A", "B"]
