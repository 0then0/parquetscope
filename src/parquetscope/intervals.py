"""Byte intervals are half-open; HTTP endpoints are inclusive."""

import re
from collections.abc import Iterable

Interval = tuple[int, int]


class UnsatisfiableRange(ValueError):
    pass


def parse_range(value: str, size: int) -> Interval:
    """Parse a *single* Range. Multi-range is ignored by the HTTP handler (200)."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or size <= 0:
        raise UnsatisfiableRange(value)
    first, last = match.groups()
    if not first:
        if not last or int(last) == 0:
            raise UnsatisfiableRange(value)
        return max(0, size - int(last)), size
    start = int(first)
    end = min(size, int(last) + 1) if last else size
    if start >= size or start >= end:
        raise UnsatisfiableRange(value)
    return start, end


def intersects(a: Interval, b: Interval) -> bool:
    return max(a[0], b[0]) < min(a[1], b[1])


def union(intervals: Iterable[Interval]) -> list[Interval]:
    result: list[Interval] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = result[-1][0], max(result[-1][1], end)
        else:
            result.append((start, end))
    return result


def coverage(intervals: Iterable[Interval]) -> int:
    return sum(end - start for start, end in union(intervals))


def complement(intervals: Iterable[Interval], size: int) -> list[Interval]:
    result = []
    cursor = 0
    for start, end in union(intervals):
        if start > cursor:
            result.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < size:
        result.append((cursor, size))
    return result


def requested_range(value: str, size: int) -> Interval | None:
    """Resolve the request without clipping an explicit inclusive upper bound.

    A server may clamp its *response*. Keep that distinction in request accounting.
    Open-ended/suffix requests need the known file size for resolution.
    """
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match:
        return None
    first, last = match.groups()
    if first and last:
        a, b = int(first), int(last) + 1
        return (a, b) if a < b else None
    try:
        return parse_range(value, size)
    except UnsatisfiableRange:
        return None
