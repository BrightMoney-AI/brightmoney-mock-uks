"""Tiny shared offset/limit pagination for the admin & dashboard list endpoints.

Every list endpoint (calls, bundles, testruns, testcases) grows without bound —
CallLog and TestResult are append-only, and bundles/cases accumulate over the
life of the mock. Paginating consistently keeps the dashboard responsive and
gives a uniform ``{count, offset, limit, has_more}`` envelope to build "load
more" / page controls against.
"""
from __future__ import annotations


def parse_page_params(request, default_limit: int = 100, max_limit: int = 1000) -> tuple[int, int]:
    try:
        limit = min(int(request.query_params.get("limit", default_limit)), max_limit)
        limit = max(limit, 1)
    except (TypeError, ValueError):
        limit = default_limit
    try:
        offset = max(int(request.query_params.get("offset", 0)), 0)
    except (TypeError, ValueError):
        offset = 0
    return limit, offset


def page_envelope(total: int, offset: int, limit: int, page_len: int) -> dict:
    return {"count": total, "offset": offset, "limit": limit,
            "has_more": offset + page_len < total}
