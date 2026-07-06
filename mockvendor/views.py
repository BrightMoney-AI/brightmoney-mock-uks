"""Catch-all serve view (design §3.1 request lifecycle).

1. receive request (method, path, headers, body)
2. look up endpoint + select scenario (matcher)
3. advance sequence cursor if needed (matcher)
4. apply delay (delay_ms)
5. serialize the canonical body to the target format
6. return HttpResponse with status + headers; write a CallLog row
"""
from __future__ import annotations

import time

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from . import matcher
from .models import CallLog
from .serializers_fmt import get_serializer


def _target_format(response, accept_header: str) -> str:
    """scenario/response explicit format > Accept header > endpoint default."""
    if response.format_id:
        return response.format.name
    if "xml" in (accept_header or "").lower():
        return "xml"
    if "json" in (accept_header or "").lower():
        return "json"
    return response.scenario.endpoint.default_format.name


def _client_ip(request) -> str:
    """Caller IP for parallel-run isolation: first X-Forwarded-For hop (nginx
    sets it, see deploy/nginx/mock_vendor.conf), else the direct peer."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return (request.META.get("REMOTE_ADDR") or "").strip()


@csrf_exempt
def serve(request, *args, **kwargs):
    method = request.method
    path = request.path
    raw_body = request.body or b""
    body = matcher.parse_body(raw_body)
    headers = {k[5:].replace("_", "-").title(): v for k, v in request.META.items() if k.startswith("HTTP_")}
    run_id = request.headers.get("X-Mock-Run-Id", "")
    client_ip = _client_ip(request)

    try:
        sel = matcher.select(method, path, headers, body, run_id=run_id, client_ip=client_ip)
    except matcher.NoEndpoint:
        CallLog.objects.create(
            endpoint=None, scenario=None, request_method=method,
            request_path=path, request_body=_safe(raw_body), response_status=404,
            request_ip=client_ip,
        )
        return HttpResponse(b'{"error":"no_endpoint"}', status=404,
                            content_type="application/json")
    except matcher.NoScenario:
        CallLog.objects.create(
            endpoint=None, scenario=None, request_method=method,
            request_path=path, request_body=_safe(raw_body), response_status=404,
            request_ip=client_ip,
        )
        return HttpResponse(b'{"error":"no_scenario"}', status=404,
                            content_type="application/json")

    resp = sel.response

    # 4) Serialize BEFORE the delay so we have the response body for the log.
    fmt_name = _target_format(resp, request.headers.get("Accept", ""))
    if resp.raw_override:
        payload = resp.raw_override.encode("utf-8")
        try:
            content_type = get_serializer(fmt_name).content_type
        except KeyError:
            content_type = "application/octet-stream"
    else:
        serializer = get_serializer(fmt_name)
        payload = serializer.serialize(resp.canonical or {}, {})
        content_type = serializer.content_type

    # 5) Log before the delay so timed-out calls are still visible in the log.
    CallLog.objects.create(
        endpoint=sel.endpoint, scenario=sel.scenario, request_method=method,
        request_path=path, request_body=_safe(raw_body),
        response_status=resp.status_code,
        response_body=_safe(payload),
        delay_applied_ms=resp.delay_ms,
        request_ip=client_ip, matched_run_id=sel.run_id,
    )

    # 6) Delay engine — client may time out here, but call is already logged.
    if resp.delay_ms:
        time.sleep(resp.delay_ms / 1000.0)

    # 7) Build and return HTTP response.
    http = HttpResponse(payload, status=resp.status_code, content_type=content_type)
    for k, v in (resp.headers or {}).items():
        http[k] = v
    return http


def _safe(raw: bytes, limit: int = 4000) -> str:
    s = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    return s[:limit]
