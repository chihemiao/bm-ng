"""Pure cross-event order-observation binding analysis."""

from dataclasses import dataclass
from typing import Any

from data.schema_order_request import order_request_event_binding


@dataclass(frozen=True, order=True, slots=True, kw_only=True)
class OrderBinding:
    venue: str
    client_order_id: str
    venue_order_id: str


def _binding_conflicts(bindings: set[OrderBinding]) -> set[str]:
    client_oids: dict[tuple[str, str], set[str]] = {}
    oid_clients: dict[tuple[str, str], set[str]] = {}
    for binding in bindings:
        client_oids.setdefault(
            (binding.venue, binding.client_order_id), set()).add(binding.venue_order_id)
        oid_clients.setdefault(
            (binding.venue, binding.venue_order_id), set()).add(binding.client_order_id)
    reasons = {
        f"order_observation:client_order_id_conflict:{venue}:{client}"
        for (venue, client), oids in client_oids.items() if len(oids) > 1
    }
    reasons.update(
        f"order_observation:venue_order_id_conflict:{venue}:{oid}"
        for (venue, oid), clients in oid_clients.items() if len(clients) > 1
    )
    return reasons


def observation_binding_evidence(
    events: list[dict[str, Any]],
) -> tuple[tuple[OrderBinding, ...], tuple[str, ...]]:
    request_indices: dict[tuple[str, str], int] = {}
    observations = []
    for index, event in enumerate(events):
        schema = event["payload_schema"]
        if schema == "order_request":
            legacy, errors = order_request_event_binding(event)
            if not legacy and not errors:
                request_indices.setdefault(
                    (event["venue"], event["client_order_id"]), index)
        elif schema == "order_observation" and event["venue_order_id"] is not None:
            observations.append((index, event))
    bindings: set[OrderBinding] = set()
    reasons = set()
    for index, event in observations:
        venue, client, oid = event["venue"], event["client_order_id"], event["venue_order_id"]
        bindings.add(OrderBinding(
            venue=venue, client_order_id=client, venue_order_id=oid))
        request_index = request_indices.get((venue, client))
        if request_index is not None and request_index >= index:
            reasons.add(f"order_observation:request_not_prior:{venue}:{client}")
    reasons.update(_binding_conflicts(bindings))
    return tuple(sorted(bindings)), tuple(sorted(reasons))
