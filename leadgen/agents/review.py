"""Human-in-the-loop gate: nothing is sent until a person approves it.

Uses LangGraph's `interrupt()`: the graph checkpoints and pauses here, the UI
or CLI collects decisions, and the run resumes with `Command(resume=...)`.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from ..state import PipelineState, event

AGENT = "Human Review"


def run(state: PipelineState) -> dict[str, Any]:
    drafts = state.get("drafts", [])
    answer = interrupt(
        {
            "type": "approval_request",
            "message": f"{len(drafts)} draft(s) are waiting for your approval.",
            "lead_ids": [d["lead_id"] for d in drafts],
        }
    )
    decisions = (answer or {}).get("decisions") or {}

    reviewed = []
    for draft in drafts:
        choice = decisions.get(draft["lead_id"]) or {}
        approved = choice.get("decision") == "approve"  # anything else is treated as a rejection
        subject = (choice.get("subject") or draft["subject"]).strip()
        body = (choice.get("body") or draft["body"]).strip()
        edited = subject != draft["subject"] or body != draft["body"]
        reviewed.append(
            {**draft, "subject": subject, "body": body, "edited_by_human": edited,
             "status": "approved" if approved else "rejected"}
        )

    approved_n = sum(1 for d in reviewed if d["status"] == "approved")
    edited_n = sum(1 for d in reviewed if d["edited_by_human"])
    message = f"Reviewer approved {approved_n} of {len(reviewed)} drafts ({edited_n} edited by hand)."
    return {"drafts": reviewed, "trace": [event(AGENT, message)]}


def route(state: PipelineState) -> str:
    return "send" if any(d["status"] == "approved" for d in state["drafts"]) else "skip"
