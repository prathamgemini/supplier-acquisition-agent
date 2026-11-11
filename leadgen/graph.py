"""The LangGraph state graph.

    prospect_search -> enrichment -> qualification --(any qualified?)--> outreach_writer
                                          |                                  |      ^
                                          | none                             v      | revise (max N rounds)
                                          |                             quality_critic
                                          |                                  | all ready
                                          |                                  v
                                          |                             human_review   <- interrupt(): waits for a person
                                          |                                  | approved        | none approved
                                          |                                  v                 |
                                          |                  send_outreach -> response_tracking|
                                          v                                  v                 v
                                          +-----------------------------> feedback -> END
"""

from __future__ import annotations

import uuid
from functools import partial
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from .agents import critic, enrichment, feedback, prospect, review, scoring, sender, tracker, writer
from .config import Settings, get_settings, load_campaign
from .llm import LLM
from .state import PipelineState

# (node id, label, one-line role) - also drives the diagram in the UI.
NODES = [
    ("prospect_search", "Prospect Search", "Load, validate and de-duplicate leads"),
    ("enrichment", "Enrichment", "Derive reachability, digital-maturity and growth signals"),
    ("qualification", "Qualification", "Rule score + LLM analyst -> qualified / nurture / disqualified"),
    ("outreach_writer", "Outreach Writer", "Draft a personalised email per qualified lead"),
    ("quality_critic", "Quality Critic", "Score drafts; send weak ones back for a rewrite"),
    ("human_review", "Human Review", "Pause until a person approves, edits or rejects"),
    ("send_outreach", "Outreach Executor", "Send approved emails (dry-run by default)"),
    ("response_tracking", "Response Tracker", "Collect opens, clicks and replies"),
    ("feedback", "Feedback", "Recommend ICP, scoring and messaging changes"),
]


def _route_after_qualification(state: PipelineState) -> str:
    return "write" if any(s["tier"] == "qualified" for s in state.get("scored", [])) else "none"


def build_graph(llm: LLM | None = None, settings: Settings | None = None, checkpointer: Any = None):
    settings = settings or get_settings()
    llm = llm or LLM(settings)

    graph = StateGraph(PipelineState)
    graph.add_node("prospect_search", prospect.run)
    graph.add_node("enrichment", enrichment.run)
    graph.add_node("qualification", partial(scoring.run, llm=llm))
    graph.add_node("outreach_writer", partial(writer.run, llm=llm))
    graph.add_node("quality_critic", partial(critic.run, llm=llm))
    graph.add_node("human_review", review.run)
    graph.add_node("send_outreach", partial(sender.run, settings=settings))
    graph.add_node("response_tracking", tracker.run)
    graph.add_node("feedback", partial(feedback.run, llm=llm))

    graph.add_edge(START, "prospect_search")
    graph.add_edge("prospect_search", "enrichment")
    graph.add_edge("enrichment", "qualification")
    graph.add_conditional_edges(
        "qualification", _route_after_qualification, {"write": "outreach_writer", "none": "feedback"}
    )
    graph.add_edge("outreach_writer", "quality_critic")
    graph.add_conditional_edges(
        "quality_critic", critic.route, {"revise": "outreach_writer", "approve": "human_review"}
    )
    graph.add_conditional_edges("human_review", review.route, {"send": "send_outreach", "skip": "feedback"})
    graph.add_edge("send_outreach", "response_tracking")
    graph.add_edge("response_tracking", "feedback")
    graph.add_edge("feedback", END)

    # A checkpointer is what makes interrupt()/resume possible.
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def new_run(campaign: dict[str, Any] | None = None, raw_leads: list[dict[str, Any]] | None = None):
    """Return (initial state, run config) for a fresh pipeline run."""
    run_id = uuid.uuid4().hex[:8]
    state: PipelineState = {"run_id": run_id, "campaign": campaign or load_campaign(), "trace": []}
    if raw_leads:
        state["raw_leads"] = raw_leads
    return state, {"configurable": {"thread_id": run_id}}


def resume_with(decisions: dict[str, dict[str, str]]) -> Command:
    """Build the resume command for the human_review interrupt."""
    # Wrapped so the payload is never empty: LangGraph ignores a falsy resume value.
    return Command(resume={"decisions": decisions})
