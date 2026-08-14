"""Gate 1 evidence extraction and window arithmetic."""

import base64
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, NamedTuple

from data.contracts import ContractError, bybit_update_gap, validate_envelope
from data.schema_dispatch import BYBIT_WIRE_SYMBOLS, DURABLE_EVENT_SCHEMAS, PAYLOAD_SCHEMAS
from data.shard import replay_records

MAX_EXPLAINED_GAP_NS = 4 * 3_600 * 1_000_000_000
MAX_EXPLAINED_GAP_PERCENT = 2
MIN_LATENCY_USABLE_HOUR_PERCENT = 95
UTC_HOUR_NS = 3_600_000_000_000
UTC_DAY_NS = 24 * UTC_HOUR_NS
GATE1_WINDOW_HOURS = 168
COMPLETION_WINDOW_HOURS = 720
EXPLAINED_FAILURE_REASONS = frozenset({
    "application_pong_timeout", "subscription_ack_timeout", "transport_disconnected",
    "transport_ping_timeout",
    "venue_down", "bybit_sequence_gap",
})
_COVERAGE_VENUES = frozenset({"hyperliquid", "bybit"})
_COVERAGE_POINT_KINDS = frozenset("hard_verified explained_failure unexplained_failure".split())
_BYBIT_BOOK_TOPICS = frozenset(
    f"orderbook.50.{symbol}" for symbol in BYBIT_WIRE_SYMBOLS.values())
_COLLECTOR_SCHEMAS = PAYLOAD_SCHEMAS - DURABLE_EVENT_SCHEMAS
_FAILURE_REASONS = {"venue_down": "venue_down", "bybit_sequence_gap": "bybit_sequence_gap"}
_SESSION_FAILURE_REASONS = EXPLAINED_FAILURE_REASONS - frozenset(_FAILURE_REASONS)


class CoveragePoint(NamedTuple):
    venue: Literal["hyperliquid", "bybit"]
    observed_ns: int
    kind: Literal["hard_verified", "explained_failure", "unexplained_failure"]
    reason: Literal[
        "application_pong_timeout", "subscription_ack_timeout", "transport_disconnected",
        "transport_ping_timeout",
        "venue_down", "bybit_sequence_gap",
    ] | None


class PairingResult(NamedTuple):
    explained_intervals: tuple[tuple[int, int], ...]
    unexplained_gap_present: bool


class BybitBarrier:
    """Single source for live and replayed Bybit sequence readiness."""

    def __init__(self) -> None:
        self.conn_id: str | None = None
        self.previous_u: dict[str, int] = {}
        self.topics: set[str] = set()
        self.gap = False

    def start(self, conn_id: str) -> None:
        if conn_id != self.conn_id:
            self.conn_id = conn_id
            self.previous_u.clear()
            self.topics.clear()
            self.gap = False

    @property
    def ready(self) -> bool:
        return not self.gap and _BYBIT_BOOK_TOPICS <= self.topics

    def observe(self, raw: bytes) -> bool:
        try:
            value = json.loads(raw)
            topic = value.get("topic", "")
            if topic not in _BYBIT_BOOK_TOPICS:
                return False
            current, kind = value["data"]["u"], value["type"]
            previous = self.previous_u.get(topic)
            missing = kind == "delta" and topic not in self.topics
            self.gap |= missing or bybit_update_gap(previous, current, kind)
            if kind == "snapshot" and not self.gap:
                self.topics.add(topic)
            self.previous_u[topic] = current
            return self.gap
        except (KeyError, TypeError, UnicodeError, ValueError) as error:
            raise ContractError("invalid Bybit sequence frame") from error


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _formal_events(root: Path):
    previous_ns, configs = -1, 0
    for raw in replay_records(root):
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ContractError("invalid collector event JSON") from error
        validate_envelope(event)
        schema = event["payload_schema"]
        valid = schema in _COLLECTOR_SCHEMAS and event.get("is_gate1_record") is True
        valid &= event["source"] == "live_public_ws" and event["recv_wall_ns"] >= previous_ns
        valid &= event["event_kind"] == ("market" if schema == "raw_frame" else "ops")
        _require(valid, "invalid formal collector record")
        previous_ns = event["recv_wall_ns"]
        is_config = schema == "collector_config"
        configs += is_config
        venue_ok = (event["venue"] == "collector" and
                    event["payload"].get("record_mode") == "formal"
                    if is_config else event["venue"] in _COVERAGE_VENUES)
        _require(venue_ok, "invalid coverage venue or config")
        if not is_config:
            yield event
    _require(configs == 1, "coverage root needs one collector config")


def _raw_payload(event: dict) -> bytes:
    try:
        return base64.b64decode(event["payload"]["raw"], validate=True)
    except (KeyError, TypeError, ValueError) as error:
        raise ContractError("invalid raw collector payload") from error


def _failure_point(event: dict, pending_gap: tuple | None):
    schema = event["payload_schema"]
    matching = schema == "bybit_sequence_gap" and pending_gap == (
        event["recv_wall_ns"], event["conn_id"])
    _require(pending_gap is None or matching, "missing Bybit sequence gap event")
    reason = (event["payload"].get("reason") if schema == "liveness_failure"
              else _FAILURE_REASONS.get(schema))
    _require(schema != "liveness_failure" or reason in _SESSION_FAILURE_REASONS,
             "invalid liveness failure reason")
    _require(schema != "bybit_sequence_gap" or matching and event["venue"] == "bybit",
             "orphan Bybit sequence gap")
    pending_gap = None if schema == "bybit_sequence_gap" else pending_gap
    if reason is None and schema != "raw_quarantine":
        return None, pending_gap
    kind = "explained_failure" if reason is not None else "unexplained_failure"
    return CoveragePoint(event["venue"], event["recv_wall_ns"], kind, reason), pending_gap


def replay_coverage_points(root: Path) -> tuple[CoveragePoint, ...]:
    _require(isinstance(root, Path), "coverage root must be a Path")
    states = {venue: dict(conn=None, ack=False, pong=False, active=False, blocked=False)
              for venue in _COVERAGE_VENUES}
    barrier, points, pending_gap = BybitBarrier(), [], None
    starters = {"hyperliquid": lambda _: None, "bybit": barrier.start}
    for event in _formal_events(root):
        schema, venue, conn = event["payload_schema"], event["venue"], event["conn_id"]
        state = states[venue]
        if schema == "subscription_send" and conn != state["conn"]:
            state.update(conn=conn, ack=False, pong=False, active=False, blocked=False)
            starters[venue](conn)
        needs_conn = schema in {"application_heartbeat", "pre_ack_frame", "raw_frame",
                                "raw_quarantine", "subscription_ack"}
        _require(not needs_conn or conn == state["conn"], "collector connection mismatch")
        failure, pending_gap = _failure_point(event, pending_gap)
        if failure is not None:
            points.append(failure)
            state.update(active=False, blocked=True)
            continue
        if state["blocked"]:
            continue
        if schema == "subscription_ack":
            _require(type(event["payload"].get("ready")) is bool, "invalid ACK readiness")
            state["ack"] |= event["payload"]["ready"]
        elif schema == "application_heartbeat" and event["payload"].get("phase") == "pong":
            state["pong"] = True
        elif venue == "bybit" and schema in {"pre_ack_frame", "raw_frame"}:
            was_gap = barrier.gap
            if barrier.observe(_raw_payload(event)) and not was_gap:
                pending_gap = event["recv_wall_ns"], conn
        ready = bool(state["conn"]) and state["ack"] and state["pong"]
        ready &= venue != "bybit" or barrier.ready
        pong_event = schema == "application_heartbeat" and event["payload"].get("phase") == "pong"
        if ready and (not state["active"] or pong_event):
            points.append(CoveragePoint(venue, event["recv_wall_ns"], "hard_verified", None))
        state["active"] = ready
    _require(pending_gap is None, "missing Bybit sequence gap event")
    return tuple(points)


def _nonnegative_exact_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _validate_coverage_point(point: object) -> None:
    _require(type(point) is CoveragePoint, "invalid coverage point")
    _require(isinstance(point.venue, str) and point.venue in _COVERAGE_VENUES,
             "invalid coverage venue")
    _require(_nonnegative_exact_int(point.observed_ns), "invalid coverage point time")
    _require(isinstance(point.kind, str) and point.kind in _COVERAGE_POINT_KINDS,
             "invalid coverage point kind")
    explained = point.kind == "explained_failure"
    valid_reason = isinstance(point.reason, str) and point.reason in EXPLAINED_FAILURE_REASONS
    _require(valid_reason if explained else point.reason is None, "invalid coverage point reason")
    _require(point.reason != "bybit_sequence_gap" or point.venue == "bybit",
             "invalid failure venue")


def _merge_intervals(intervals: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start_ns, end_ns in sorted(intervals):
        if merged and start_ns <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(merged[-1][1], end_ns)
        else:
            merged.append((start_ns, end_ns))
    return tuple(merged)


def pair_explained_intervals(
    points: Sequence[CoveragePoint],
    *,
    window_start_ns: int,
    window_end_ns: int,
) -> PairingResult:
    _require(_nonnegative_exact_int(window_start_ns), "invalid window_start_ns")
    _require(_nonnegative_exact_int(window_end_ns), "invalid window_end_ns")
    _require(window_end_ns > window_start_ns, "invalid pairing window")
    _require(isinstance(points, Sequence), "invalid coverage points")
    last_verified: dict[str, int] = {}
    pending: dict[str, list[int | None]] = {venue: [] for venue in _COVERAGE_VENUES}
    intervals: list[tuple[int, int]] = []
    unexplained, previous_ns = False, -1
    for point in points:
        _validate_coverage_point(point)
        _require(point.observed_ns >= previous_ns, "coverage point order moved backwards")
        previous_ns = point.observed_ns
        if point.observed_ns >= window_end_ns:
            continue
        if point.kind == "hard_verified":
            starts = pending[point.venue]
            if point.observed_ns > window_start_ns:
                for start_ns in starts:
                    if start_ns is None:
                        unexplained = True
                    elif (clipped := max(start_ns, window_start_ns)) < point.observed_ns:
                        intervals.append((clipped, point.observed_ns))
            starts.clear()
            last_verified[point.venue] = point.observed_ns
        elif point.kind == "explained_failure":
            pending[point.venue].append(last_verified.get(point.venue))
        elif point.observed_ns >= window_start_ns:
            unexplained = True
    unexplained |= any(pending.values())
    return PairingResult(_merge_intervals(intervals), unexplained)


def eligible_utc_hours(
    *,
    start_ns: int,
    end_ns: int,
    window_kind: Literal["gate1_7d", "completion_30d"],
) -> int:
    """Validate one frozen half-open UTC coverage window and return its hours."""
    _require(type(start_ns) is int, "invalid start_ns type")
    _require(type(end_ns) is int, "invalid end_ns type")
    _require(start_ns >= 0 and end_ns > start_ns, "invalid UTC window range")
    _require(
        start_ns % UTC_HOUR_NS == 0 and end_ns % UTC_HOUR_NS == 0,
        "UTC hour alignment required",
    )
    _require(
        type(window_kind) is str
        and window_kind in {"gate1_7d", "completion_30d"},
        "invalid window_kind",
    )
    expected_hours = (
        GATE1_WINDOW_HOURS
        if window_kind == "gate1_7d"
        else COMPLETION_WINDOW_HOURS
    )
    actual_hours = (end_ns - start_ns) // UTC_HOUR_NS
    _require(actual_hours == expected_hours, "invalid window duration")
    _require(
        window_kind != "completion_30d" or start_ns % UTC_DAY_NS == 0,
        "completion window must be UTC day aligned",
    )
    return actual_hours


def window_coverage_ok(
    *,
    window_duration_ns: int,
    max_single_gap_ns: int,
    explained_gap_total_ns: int,
    unexplained_gap_present: bool,
    usable_hours: int,
    eligible_hours: int,
) -> bool:
    """Apply frozen thresholds without classifying gaps, hours, or recovery streaks."""
    numbers = {
        "window_duration_ns": window_duration_ns,
        "max_single_gap_ns": max_single_gap_ns,
        "explained_gap_total_ns": explained_gap_total_ns,
        "usable_hours": usable_hours,
        "eligible_hours": eligible_hours,
    }
    for name, value in numbers.items():
        _require(_nonnegative_exact_int(value), f"invalid {name}")
    _require(window_duration_ns > 0, "invalid window_duration_ns")
    _require(eligible_hours > 0, "invalid eligible_hours")
    _require(type(unexplained_gap_present) is bool, "invalid unexplained_gap_present")
    _require(max_single_gap_ns <= explained_gap_total_ns, "gap maximum exceeds total")
    _require(explained_gap_total_ns <= window_duration_ns, "gap total exceeds window")
    _require(bool(max_single_gap_ns) == bool(explained_gap_total_ns), "incomplete gap aggregate")
    _require(usable_hours <= eligible_hours, "usable hours exceed eligible hours")
    return all((
        not unexplained_gap_present,
        max_single_gap_ns <= MAX_EXPLAINED_GAP_NS,
        explained_gap_total_ns * 100 <= window_duration_ns * MAX_EXPLAINED_GAP_PERCENT,
        usable_hours * 100 >= eligible_hours * MIN_LATENCY_USABLE_HOUR_PERCENT,
    ))
