import pytest

from parquetscope.intervals import (
    UnsatisfiableRange,
    complement,
    coverage,
    intersects,
    parse_range,
    union,
)


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        ("bytes=0-0", 100, (0, 1)),
        ("bytes=3-9", 100, (3, 10)),
        ("bytes=99-1000", 100, (99, 100)),
        ("bytes=42-", 100, (42, 100)),
        ("bytes=-10", 100, (90, 100)),
        ("bytes=-999", 100, (0, 100)),
        ("bytes=9007199254740993-9007199254740999", 10**18, (9007199254740993, 9007199254741000)),
    ],
)
def test_http_to_half_open(header, size, expected):
    assert parse_range(header, size) == expected


@pytest.mark.parametrize(
    "header", ["bytes=100-", "bytes=9-2", "bytes=-0", "bytes=-", "bad", "bytes=0-2,4-6"]
)
def test_unsatisfiable(header):
    with pytest.raises(UnsatisfiableRange):
        parse_range(header, 100)


def test_empty_file():
    with pytest.raises(UnsatisfiableRange):
        parse_range("bytes=0-", 0)


def test_overlap_containment_adjacent_disjoint():
    intervals = [(0, 10), (3, 7), (8, 12), (12, 15), (20, 23)]
    assert union(intervals) == [(0, 15), (20, 23)]
    assert coverage(intervals) == 18
    assert sum(b - a for a, b in intervals) == 24
    assert intersects((0, 10), (9, 12))
    assert not intersects((0, 10), (10, 20))
    assert complement(intervals, 30) == [(15, 20), (23, 30)]


def test_requested_extent_is_not_response_clamping():
    from parquetscope.intervals import requested_range

    assert requested_range("bytes=90-199", 100) == (90, 200)
    assert parse_range("bytes=90-199", 100) == (90, 100)
    assert requested_range("bytes=200-299", 100) == (200, 300)
    assert requested_range("bytes=1-", 100) == (1, 100)
    assert requested_range("bytes=-5", 100) == (95, 100)
    assert requested_range("bytes=3-1", 100) is None
