import http.client
import socket
import threading
from dataclasses import fields
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from inspect import Parameter, signature
from urllib.request import Request, urlopen

import pytest

from data import shard
from execution import submission
from execution.orders import make_order_intent, order_request_record

INTENT = make_order_intent(
    "funding-carry", "git-deadbeef", 100, "hyperliquid", symbol="BTC", side="buy",
    quantity=Decimal("1"))
REQUEST = order_request_record(
    INTENT, 110, account_digest="a" * 64, lease_epoch=1,
    writer_instance_id="writer-one", wallet_fingerprint="b" * 64, allocated_nonce=7)

def _fields():
    return submission.ObservedFields(
        venue_order_id="7", status="open", observation_source="submission_response",
        venue_time_ms=None)


def _wrap(transport, mapper, recorder):
    return submission.observe_transport(
        transport, success_mapper=mapper, observation_recorder=recorder,
        observed_ns=120, conn_id="conn-1", boot_id="boot-1", recv_wall_ns=120,
        recv_mono_ns=90, source="execution", seq_within_boot=3)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok" if self.path == "/" else b"x")
        self.wfile.flush()
        if self.path != "/":
            self.connection.shutdown(socket.SHUT_RDWR)

    def log_message(self, *_args):
        pass


@pytest.fixture
def server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


def _post(url):
    with urlopen(Request(url, data=b"{}", method="POST"), timeout=1) as response:
        return response.read()


def test_observer_surface_is_typed_and_success_is_durable_and_opaque(tmp_path, server):
    parameters = signature(submission.observe_transport).parameters
    assert tuple(parameters) == (
        "transport", "success_mapper", "observation_recorder", "observed_ns", "conn_id",
        "boot_id", "recv_wall_ns", "recv_mono_ns", "source", "seq_within_boot",
    )
    assert parameters["transport"].kind is Parameter.POSITIONAL_OR_KEYWORD
    assert all(p.kind is Parameter.KEYWORD_ONLY for p in tuple(parameters.values())[1:])
    assert tuple(f.name for f in fields(submission.ObservedFields)) == (
        "venue_order_id", "status", "observation_source", "venue_time_ms")
    assert submission.ObservedFields.__dataclass_params__.frozen
    assert submission.ObservedFields.__slots__
    with pytest.raises(TypeError):
        submission.ObservedFields("7", "open", "submission_response", None)
    writer = shard.ShardWriter(tmp_path, boot_id="boot-1")
    recorded, raised = [], []
    transport = _wrap(lambda _request: _post(server + "/"), lambda *_: _fields(),
                      writer.append_event)
    result = transport(REQUEST)
    def transport(_request):
        try:
            return _post(server + "/failure")
        except http.client.IncompleteRead as error:
            raised.append(error)
            raise
    with pytest.raises(http.client.IncompleteRead) as caught:
        _wrap(transport, lambda *_: _fields(), recorded.append)(REQUEST)
    writer.close()
    event = shard.replay_event_window(tmp_path, 0, 200).events[0]
    assert result == b"ok"
    assert event["venue"] == "hyperliquid" and event["payload"]["status"] == "open"
    assert event["client_order_id"] == REQUEST.client_order_id
    event = recorded[0]
    assert caught.value is raised[0]
    assert event["venue"] == "hyperliquid" and event["venue_order_id"] is None
    assert event["client_order_id"] == REQUEST.client_order_id
    assert event["payload"] == {"status": "unknown", "source": "no_venue_response",
                                "observed_ns": 120, "venue_time_ms": None}


def test_mapper_failure_keeps_result_and_skips_recorder():
    result, recorded, error = object(), [], ValueError("unknown response")
    def mapper(*_args):
        raise error

    with pytest.raises(submission.ObservationMappingError) as caught:
        _wrap(lambda _request: result, mapper, recorded.append)(REQUEST)
    assert (caught.value.result, caught.value.__cause__, recorded) == (result, error, [])


@pytest.mark.parametrize("transport_fails", [False, True])
def test_recorder_failure_reports_completed_transport_outcome(transport_fails):
    result, transport_error, recorder_error = object(), OSError("transport"), OSError("record")
    def transport(_request):
        if transport_fails:
            raise transport_error
        return result
    def recorder(_event):
        raise recorder_error

    with pytest.raises(submission.ObservationRecordingError) as caught:
        _wrap(transport, lambda *_: _fields(), recorder)(REQUEST)
    assert caught.value.outcome == ("failure" if transport_fails else "success")
    assert caught.value.result is (None if transport_fails else result)
    assert caught.value.transport_error is (transport_error if transport_fails else None)
    assert caught.value.__cause__ is recorder_error
