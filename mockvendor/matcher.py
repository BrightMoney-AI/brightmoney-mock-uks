"""Scenario selection + response resolution (design §3.1, §3.2).

Selection is keyed by the endpoint that was called (method + path). When several
enabled scenarios target the same endpoint, an optional discriminator
(match_key / match_value, a JSONPath such as ``$.test_path``) narrows the set,
then ``priority`` (highest wins) breaks any remaining tie.

For a sequence scenario (``is_sequence``), successive calls advance a per-scenario
cursor across its ordered Response rows (Nth-call behaviour).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from django.db import transaction

from .models import Endpoint, Response, Scenario


class NoEndpoint(Exception):
    """No endpoint configured for the requested method+path."""


class NoScenario(Exception):
    """Endpoint exists but no enabled scenario matched."""


def _json_path_get(data, path: str):
    """Resolve a tiny JSONPath subset: ``$.a.b`` against a dict/list tree."""
    if not path.startswith("$"):
        # bare key
        return data.get(path) if isinstance(data, dict) else None
    cur = data
    for part in path.lstrip("$").lstrip(".").split("."):
        if part == "":
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _discriminator_matches(sc: Scenario, body: dict, headers: dict) -> bool:
    if not sc.match_key:
        return True  # no discriminator -> always eligible
    key = sc.match_key
    # Header refs: "$header.X-Foo" ; otherwise treat as a body JSONPath.
    if key.startswith("$header."):
        hv = headers.get(key[len("$header."):])
        return str(hv) == sc.match_value
    found = _json_path_get(body, key)
    return found is not None and str(found) == sc.match_value


@dataclass
class Selection:
    endpoint: Endpoint
    scenario: Scenario
    response: Response


def select(method: str, path: str, headers: dict, body: dict, run_id: str = "") -> Selection:
    """Find the endpoint, choose the scenario, resolve the response.

    Raises ``NoEndpoint`` / ``NoScenario`` when nothing matches.
    """
    try:
        endpoint = Endpoint.objects.get(method=method.upper(), path_pattern=path, enabled=True)
    except Endpoint.DoesNotExist as exc:
        raise NoEndpoint(f"{method} {path}") from exc

    qs = endpoint.scenarios.filter(enabled=True)
    if run_id:
        qs = qs.filter(run_id=run_id)
    candidates = [sc for sc in qs if _discriminator_matches(sc, body, headers)]
    if not candidates:
        raise NoScenario(f"{method} {path}")

    # Prefer scenarios that actually carry a discriminator (more specific),
    # then highest priority, then most-recently created.
    candidates.sort(key=lambda s: (bool(s.match_key), s.priority, s.id), reverse=True)
    scenario = candidates[0]
    response = _resolve_response(scenario)
    return Selection(endpoint=endpoint, scenario=scenario, response=response)


@transaction.atomic
def _resolve_response(scenario: Scenario) -> Response:
    responses = list(scenario.responses.all())  # ordered by seq_index, id
    if not responses:
        raise NoScenario(f"scenario {scenario.name!r} has no responses")

    if not scenario.is_sequence:
        return responses[0]

    # Stateful sequence: serve the response at the cursor, then advance.
    # Lock the row so concurrent calls advance the cursor safely.
    locked = Scenario.objects.select_for_update().get(pk=scenario.pk)
    idx = min(locked.seq_cursor, len(responses) - 1)
    chosen = responses[idx]
    if locked.seq_cursor < len(responses) - 1:
        locked.seq_cursor += 1
        locked.save(update_fields=["seq_cursor"])
    return chosen


def parse_body(raw: bytes | str) -> dict:
    """Best-effort parse of a request body into a dict for discriminator matching."""
    if not raw:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_": parsed}
    except (ValueError, TypeError):
        return {}
