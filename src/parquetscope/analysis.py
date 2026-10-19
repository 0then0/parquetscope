from .intervals import coverage, union
from .models import Capture, HTTPObservation, Metrics, ServerObservation, span


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
        if o.declared_body_bytes != s.declared_body_bytes:
            return False
    return True
