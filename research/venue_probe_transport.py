"""Redacted HTTP transport for the user-run Hyperliquid venue probe."""

import json
import time
from collections.abc import Mapping
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from research.venue_probe import normalize_venue_status

TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


class NoopObservation(NamedTuple):
    http_status: int | None
    venue_status: str
    elapsed_ms: int


def _validated_base_url(base_url: str) -> str:
    if base_url == TESTNET_API_URL:
        return base_url
    parsed = urlparse(base_url)
    is_loopback = (
        parsed.scheme == "http"
        and parsed.hostname in _LOOPBACK_HOSTS
        and parsed.port is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.path in ("", "/")
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )
    if not is_loopback:
        raise ValueError("probe base_url is not allowlisted")
    return base_url.rstrip("/")


def _elapsed_ms(start_ns: int) -> int:
    return (time.monotonic_ns() - start_ns) // 1_000_000


def _observation(
    http_status: int | None, venue_status: str, start_ns: int
) -> NoopObservation:
    return NoopObservation(http_status, venue_status, _elapsed_ms(start_ns))


def _body_status(body: bytes) -> str:
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "absent"
    status_field = data.get("status") if isinstance(data, Mapping) else None
    return normalize_venue_status(status_field=status_field)


def post_action(
    *, base_url: str, payload: Mapping[str, object], timeout_s: float
) -> NoopObservation:
    """POST one signed action while returning only closed, redacted primitives."""
    endpoint = _validated_base_url(base_url) + "/exchange"
    start_ns = time.monotonic_ns()
    try:
        body = json.dumps(payload).encode()
        request = Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=timeout_s) as result:
            return _observation(result.status, _body_status(result.read()), start_ns)
    except HTTPError as error:
        return _observation(error.code, "absent", start_ns)
    except Exception:
        return _observation(None, "absent", start_ns)
