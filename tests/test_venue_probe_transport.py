import inspect
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import research.venue_probe_transport as transport

SENTINEL = "signature-response-sentinel"


@contextmanager
def _server(*, status=200, body=b"{}", delay_s=0.0, drop=False):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if drop:
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            time.sleep(delay_s)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


def _post(base_url, *, timeout_s=1.0):
    return transport.post_action(
        base_url=base_url,
        payload={"action": {"type": "noop"}, "signature": SENTINEL},
        timeout_s=timeout_s,
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (json.dumps({"status": "ok"}).encode(), "ok"),
        (json.dumps({"status": "err", "response": SENTINEL}).encode(), "err"),
        (json.dumps({"response": SENTINEL}).encode(), "absent"),
        (b"not-json", "absent"),
        (json.dumps({"status": "rejected"}).encode(), "absent"),
    ],
)
def test_real_local_response_exposes_only_closed_observation_fields(body, expected):
    with _server(body=body) as base_url:
        observation = _post(base_url)
    assert observation.http_status == 200
    assert observation.venue_status == expected
    assert type(observation.elapsed_ms) is int and observation.elapsed_ms >= 0
    assert SENTINEL not in repr(observation)


def test_http_failure_preserves_status_without_response_prose():
    with _server(status=500, body=SENTINEL.encode()) as base_url:
        observation = _post(base_url)
    assert observation.http_status == 500
    assert observation.venue_status == "absent"
    assert SENTINEL not in repr(observation)


@pytest.mark.parametrize(
    ("delay_s", "timeout_s", "drop"),
    [(0.0, 1.0, True), (0.05, 0.005, False)],
)
def test_real_transport_faults_return_only_an_absent_observation(
    delay_s, timeout_s, drop
):
    with _server(delay_s=delay_s, drop=drop) as base_url:
        observation = _post(base_url, timeout_s=timeout_s)
    assert observation.http_status is None
    assert observation.venue_status == "absent"
    assert SENTINEL not in repr(observation)


@pytest.mark.parametrize(
    "base_url", ["https://example.invalid", "http://127.0.0.1.invalid:80"]
)
def test_non_testnet_non_loopback_urls_are_rejected_without_echoing_input(base_url):
    with pytest.raises(ValueError, match="not allowlisted") as raised:
        _post(base_url)
    assert base_url not in str(raised.value)


def test_transport_surface_and_source_are_closed():
    assert transport._validated_base_url(transport.TESTNET_API_URL) == transport.TESTNET_API_URL
    assert transport.NoopObservation._fields == (
        "http_status",
        "venue_status",
        "elapsed_ms",
    )
    assert tuple(inspect.signature(transport.post_action).parameters) == (
        "base_url",
        "payload",
        "timeout_s",
    )
    source = inspect.getsource(transport)
    assert "api.hyperliquid.xyz" not in source
    assert "response[" not in source
