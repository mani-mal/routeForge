"""Local tools over intentionally synthetic support data."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool


TICKETS: dict[str, dict[str, Any]] = {
    "TCK-1001": {
        "ticket_id": "TCK-1001",
        "customer_id": "CUS-204",
        "subject": "Cannot connect after router replacement",
        "summary": "Customer replaced a home router and reports an authentication loop.",
        "service": "fiber-internet",
        "region": "Austin",
        "opened_hours_ago": 6,
    },
    "TCK-1002": {
        "ticket_id": "TCK-1002",
        "customer_id": "CUS-319",
        "subject": "Intermittent voice service",
        "summary": "Calls drop after roughly ten minutes on two handsets.",
        "service": "hosted-voice",
        "region": "Chicago",
        "opened_hours_ago": 30,
    },
}

CUSTOMERS: dict[str, dict[str, Any]] = {
    "CUS-204": {
        "customer_id": "CUS-204",
        "name": "Avery Morgan",
        "email": "avery.morgan@example.test",
        "phone": "+1-202-555-0147",
        "plan": "Fiber Pro 1G",
        "account_status": "active",
        "router_mac": "02:00:5E:10:00:01",
    },
    "CUS-319": {
        "customer_id": "CUS-319",
        "name": "Jordan Lee",
        "email": "jordan.lee@example.test",
        "phone": "+1-202-555-0189",
        "plan": "Voice Business",
        "account_status": "active",
        "router_mac": "02:00:5E:10:00:02",
    },
}

SERVICE_STATUS = {
    ("fiber-internet", "Austin"): {
        "state": "operational",
        "active_incidents": 0,
        "last_change": "2026-07-30T15:00:00Z",
    },
    ("hosted-voice", "Chicago"): {
        "state": "degraded",
        "active_incidents": 1,
        "incident": "Elevated SIP registration churn; mitigation in progress.",
        "last_change": "2026-08-02T13:20:00Z",
    },
}


def public_ticket(ticket_id: str) -> dict[str, Any]:
    """Return ticket details that do not include customer contact data."""
    ticket = TICKETS.get(ticket_id.upper())
    if ticket is None:
        raise ValueError(f"Unknown synthetic ticket {ticket_id!r}")
    return dict(ticket)


@tool
def get_customer_record(customer_id: str) -> str:
    """Get the synthetic customer's account and contact record."""
    record = CUSTOMERS.get(customer_id.upper())
    if record is None:
        return json.dumps({"error": f"unknown customer {customer_id}"})
    return json.dumps(record, sort_keys=True)


@tool
def get_service_status(service: str, region: str) -> str:
    """Get current synthetic service health for a product and region."""
    status = SERVICE_STATUS.get((service, region), {"state": "unknown", "active_incidents": 0})
    return json.dumps({"service": service, "region": region, **status}, sort_keys=True)

