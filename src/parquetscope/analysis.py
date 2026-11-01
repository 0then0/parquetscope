from .intervals import coverage, union
from .models import Capture, HTTPObservation, Metrics, ServerObservation, span
from .models_v2 import (
    FileHTTPObservation,
    FileInput,
    FileMetrics,
    FileServerObservation,
    SetMetrics,
)


def derive_metrics(
    observations: list[HTTPObservation], server: list[ServerObservation], capture: Capture
) -> Metrics:
    gets = [o for o in observations if o.method == "GET"]
    requested = [o.requested_span.pair() for o in gets if o.requested_span]
    confirmed = [o.response_span.pair() for o in gets if o.response_span]
    known_requests = all(o.requested_span is not None for o in gets)
    known_responses = all(o.response_span is not None for o in gets)
    known_lengths = all(o.declared_body_bytes is not None for o in gets)
    reasons = []
    if not capture.complete:
        reasons.append(
            capture.reason or "Incomplete capture; metrics describe observed subset only"
        )
    if not known_requests:
        reasons.append("Some GETs have no interpretable single requested range")
    if not known_responses:
        reasons.append("Some GET responses have no confirmed file interval")
    if not known_lengths:
        reasons.append("Some GET responses have no declared Content-Length")
    total = sum(b - a for a, b in confirmed)
    unique = coverage(confirmed)
    return Metrics(
        get_count=len(gets),
        head_count=sum(o.method == "HEAD" for o in observations),
        requested_range_bytes=str(sum(b - a for a, b in requested)) if known_requests else None,
        confirmed_range_bytes=str(total) if known_responses else None,
        declared_body_bytes=(
            str(sum(int(o.declared_body_bytes) for o in gets if o.declared_body_bytes is not None))
            if known_lengths
            else None
        ),
        server_written_bytes=str(sum(int(o.written_body_bytes) for o in server)),
        unique_coverage_bytes=str(unique) if known_responses else None,
        repeated_coverage_bytes=str(total - unique) if known_responses else None,
        coverage=[span(a, b) for a, b in union(confirmed)] if known_responses else None,
        totals_scope="complete" if capture.complete else "observed_subset",
        unknown_reasons=reasons,
    )


def reconcile(observations: list[HTTPObservation], server: list[ServerObservation]) -> bool:
    """Response IDs distinguish repeated and concurrent identical requests."""
    return _reconcile(observations, server, lambda _o, _s: True)


def reconcile_set(
    observations: list[FileHTTPObservation], server: list[FileServerObservation]
) -> bool:
    """As `reconcile`, and both independent sources must attribute a request to the same file."""
    return _reconcile(observations, server, lambda o, s: o.file_id == s.file_id)


def _reconcile(observations, server, same_file) -> bool:
    by_id = {o.id: o for o in server}
    if (
        len(by_id) != len(server)
        or len(server) != len(observations)
        or len({o.id for o in observations}) != len(observations)
    ):
        return False
    for o in observations:
        s = by_id.get(o.id)
        if not s or (o.method, o.requested_range, o.response_span, o.status) != (
            s.method,
            s.requested_range,
            s.response_span,
            s.status,
        ):
            return False
        if o.declared_body_bytes != s.declared_body_bytes or not same_file(o, s):
            return False
    return True


def _file_metrics(
    file_id: str, observations: list[FileHTTPObservation], server: list[FileServerObservation]
) -> FileMetrics:
    """Intervals belong to one file; callers never pass observations of several files."""
    gets = [o for o in observations if o.method == "GET"]
    heads = len(observations) - len(gets)
    requested = [o.requested_span.pair() for o in gets if o.requested_span]
    confirmed = [o.response_span.pair() for o in gets if o.response_span]
    known_requests = all(o.requested_span is not None for o in gets)
    known_responses = all(o.response_span is not None for o in gets)
    known_lengths = all(o.declared_body_bytes is not None for o in gets)
    reasons = []
    if not known_requests:
        reasons.append("Some GETs have no interpretable single requested range")
    if not known_responses:
        reasons.append("Some GET responses have no confirmed file interval")
    if not known_lengths:
        reasons.append("Some GET responses have no declared Content-Length")
    total = sum(b - a for a, b in confirmed)
    unique = coverage(confirmed)
    # Either independent source is enough to say a request for the file was observed.
    server_gets = any(s.method == "GET" for s in server)
    server_heads = any(s.method == "HEAD" for s in server)
    if gets or server_gets:
        access = "get"
    elif heads or server_heads:
        access = "head_only"
    else:
        access = "none"
    return FileMetrics(
        file_id=file_id,
        observed_access=access,
        get_count=len(gets),
        head_count=heads,
        requested_range_bytes=str(sum(b - a for a, b in requested)) if known_requests else None,
        confirmed_range_bytes=str(total) if known_responses else None,
        declared_body_bytes=(
            str(sum(int(o.declared_body_bytes) for o in gets if o.declared_body_bytes is not None))
            if known_lengths
            else None
        ),
        server_written_bytes=str(sum(int(o.written_body_bytes) for o in server)),
        unique_coverage_bytes=str(unique) if known_responses else None,
        repeated_coverage_bytes=str(total - unique) if known_responses else None,
        coverage=[span(a, b) for a, b in union(confirmed)] if known_responses else None,
        unknown_reasons=reasons,
    )


def _sum_known(values: list[str | None]) -> str | None:
    return None if any(v is None for v in values) else str(sum(int(v) for v in values))


def derive_set_metrics(
    files: list[FileInput],
    observations: list[FileHTTPObservation],
    server: list[FileServerObservation],
    capture: Capture,
) -> SetMetrics:
    """Per-file metrics grouped by file_id in one pass; set totals are sums of file totals."""
    by_file: dict[str, list[FileHTTPObservation]] = {f.id: [] for f in files}
    server_by_file: dict[str, list[FileServerObservation]] = {f.id: [] for f in files}
    for o in observations:
        by_file[o.file_id].append(o)
    for s in server:
        server_by_file[s.file_id].append(s)
    per_file = [_file_metrics(f.id, by_file[f.id], server_by_file[f.id]) for f in files]
    reasons = []
    if not capture.complete:
        reasons.append(
            capture.reason or "Incomplete capture; metrics describe observed subset only"
        )
    for m in per_file:
        reasons.extend(r for r in m.unknown_reasons if r not in reasons)
    return SetMetrics(
        files_without_observed_access=sum(m.observed_access == "none" for m in per_file),
        files_head_only=sum(m.observed_access == "head_only" for m in per_file),
        files_with_get=sum(m.observed_access == "get" for m in per_file),
        get_count=sum(m.get_count for m in per_file),
        head_count=sum(m.head_count for m in per_file),
        requested_range_bytes=_sum_known([m.requested_range_bytes for m in per_file]),
        confirmed_range_bytes=_sum_known([m.confirmed_range_bytes for m in per_file]),
        declared_body_bytes=_sum_known([m.declared_body_bytes for m in per_file]),
        server_written_bytes=str(sum(int(m.server_written_bytes) for m in per_file)),
        unique_coverage_bytes=_sum_known([m.unique_coverage_bytes for m in per_file]),
        repeated_coverage_bytes=_sum_known([m.repeated_coverage_bytes for m in per_file]),
        totals_scope="complete" if capture.complete else "observed_subset",
        unknown_reasons=reasons,
        files=per_file,
    )
