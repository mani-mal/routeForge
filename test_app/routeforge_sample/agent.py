"""A LangChain support-investigation workflow that calls RouteForge per step."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableLambda, RunnableSequence

from routeforge_sample.routeforge import RouteForgeChatModel
from routeforge_sample.tools import get_customer_record, get_service_status, public_ticket


PLANNER_SYSTEM = """You are the planning stage of a customer-support investigation agent.
Given public ticket data, write a short investigation plan. Explain what the customer-record
and service-status tools can establish. Do not invent facts or claim the tools already ran."""

SYNTHESIS_SYSTEM = """You are a customer-support investigation agent. Use only the supplied
ticket and tool results. Produce a concise draft with: likely cause, supporting evidence,
recommended next actions, and escalation conditions. Treat all records as synthetic.
Avoid repeating contact details unless operationally necessary."""

REVIEW_SYSTEM = """You are the final quality reviewer for a customer-support investigation.
Return a clear final recommendation. Check that claims are supported by the evidence, remove
unnecessary personal details, and retain useful escalation criteria. Do not mention this review."""


@dataclass(frozen=True)
class InvestigationResult:
    ticket_id: str
    answer: str
    calls: tuple[dict[str, Any], ...]

    @property
    def total_cost_usd(self) -> float:
        return sum(float(call.get("cost", {}).get("total_usd", 0.0)) for call in self.calls)


class SupportInvestigator:
    """Investigate synthetic tickets with local tools and three routed LLM calls."""

    def __init__(self, model: RouteForgeChatModel) -> None:
        self.model = model
        # RunnableSequence makes the LangChain orchestration explicit while the
        # individual stages remain easy to inspect and test.
        self._workflow: RunnableSequence = (
            RunnableLambda(self._plan)
            | RunnableLambda(self._gather_evidence)
            | RunnableLambda(self._synthesize)
            | RunnableLambda(self._review)
        )

    def investigate(self, ticket_id: str) -> InvestigationResult:
        audit_start = len(self.model.audit_records)
        # One correlation id for the whole run so RouteForge groups the three
        # routed calls under one session (console tree + Langfuse Session). The
        # uuid nonce makes each run distinct — two runs of the same ticket are
        # two sessions.
        self.model.session_id = f"inv-{ticket_id.lower()}-{uuid.uuid4().hex[:8]}"
        try:
            state = self._workflow.invoke({"ticket_id": ticket_id.upper()})
        finally:
            self.model.session_id = None
        calls = self.model.audit_records[audit_start:]
        return InvestigationResult(ticket_id=state["ticket_id"], answer=state["answer"], calls=calls)

    def _plan(self, state: dict[str, Any]) -> dict[str, Any]:
        ticket = public_ticket(state["ticket_id"])
        response = self.model.invoke(
            [SystemMessage(PLANNER_SYSTEM), HumanMessage(json.dumps(ticket, sort_keys=True))]
        )
        return {**state, "ticket": ticket, "plan": self._text(response)}

    @staticmethod
    def _gather_evidence(state: dict[str, Any]) -> dict[str, Any]:
        ticket = state["ticket"]
        customer = get_customer_record.invoke({"customer_id": ticket["customer_id"]})
        service = get_service_status.invoke(
            {"service": ticket["service"], "region": ticket["region"]}
        )
        return {**state, "customer_record": customer, "service_status": service}

    def _synthesize(self, state: dict[str, Any]) -> dict[str, Any]:
        # Tool outputs are deliberately included in the NEXT RouteForge call.
        # That lets its input gate detect data the initial request never held.
        evidence = {
            "ticket": state["ticket"],
            "plan": state["plan"],
            "tool_results": {
                "get_customer_record": json.loads(state["customer_record"]),
                "get_service_status": json.loads(state["service_status"]),
            },
        }
        response = self.model.invoke(
            [SystemMessage(SYNTHESIS_SYSTEM), HumanMessage(json.dumps(evidence, sort_keys=True))]
        )
        return {**state, "draft": self._text(response)}

    def _review(self, state: dict[str, Any]) -> dict[str, Any]:
        response = self.model.invoke(
            [
                SystemMessage(REVIEW_SYSTEM),
                HumanMessage(
                    json.dumps(
                        {
                            "ticket_id": state["ticket_id"],
                            "service_status": json.loads(state["service_status"]),
                            "draft": state["draft"],
                        },
                        sort_keys=True,
                    )
                ),
            ]
        )
        return {**state, "answer": self._text(response)}

    @staticmethod
    def _text(message: AIMessage) -> str:
        if not isinstance(message.content, str):
            raise TypeError("Expected a text response from RouteForge")
        return message.content

