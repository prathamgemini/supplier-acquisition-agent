"""Shared graph state and the structured outputs the LLM must return."""

from __future__ import annotations

import operator
from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field, model_validator


class PipelineState(TypedDict, total=False):
    run_id: str
    campaign: dict[str, Any]
    raw_leads: list[dict[str, Any]]  # optional: leads supplied by the caller (e.g. CSV upload)
    leads: list[dict[str, Any]]
    intake_rejects: list[dict[str, Any]]
    scored: list[dict[str, Any]]
    drafts: list[dict[str, Any]]
    revision_round: int
    send_results: list[dict[str, Any]]
    engagement: dict[str, Any]
    feedback: dict[str, Any]
    # Append-only audit log: every agent explains what it did and why.
    trace: Annotated[list[dict[str, Any]], operator.add]


def event(agent: str, message: str, level: str = "info") -> dict[str, Any]:
    return {
        "ts": datetime.now().strftime("%H:%M:%S"),
        "agent": agent,
        "message": message,
        "level": level,
    }


# --- Structured LLM outputs -------------------------------------------------
# Kept flat (no optional fields) so they work with Groq's JSON-schema mode.


class LeadAssessment(BaseModel):
    """Qualification analyst's judgement of one lead."""

    fit_score: int = Field(description="0-100: how well this supplier fits the ICP and would benefit from the offer")
    rationale: str = Field(description="Two sentences explaining the score, citing facts from the lead record")
    pain_points: list[str] = Field(description="1-3 concrete business problems this supplier likely has")
    personalization_hook: str = Field(description="One specific fact about this supplier to open an email with")
    likely_objection: str = Field(description="The most probable reason they would say no")
    recommended_action: Literal["outreach_now", "nurture", "disqualify"]


class EmailDraft(BaseModel):
    subject: str = Field(description="Subject line, under 60 characters, no clickbait")
    body: str = Field(description="Plain-text email body including greeting, sign-off and opt-out line")


class Critique(BaseModel):
    """Quality reviewer's scores for one draft. Each score is 1-10."""

    personalization: int
    clarity: int
    value_proposition: int
    call_to_action: int
    compliance: int = Field(description="Honest claims, no invented facts, respectful, has an opt-out line")
    issues: list[str] = Field(description="Specific problems found; empty list if none")
    revision_instructions: str = Field(description="What the writer should change; empty string if nothing")

    @model_validator(mode="before")
    @classmethod
    def _tolerate_model_quirks(cls, data):
        """The model sometimes nests the five scores under "score"/"scores", returns one overall
        "score" instead of five, or writes "None" for empty. Accept all of those."""
        if isinstance(data, dict):
            for key in ("score", "scores"):
                if isinstance(data.get(key), dict):
                    data = {**data.pop(key), **data}
                elif isinstance(data.get(key), (int, float)):  # one overall score: apply it to every rubric item
                    overall = round(data.pop(key))
                    rubric = ("personalization", "clarity", "value_proposition", "call_to_action", "compliance")
                    data = {**{name: overall for name in rubric}, **data}
            issues = data.get("issues") or []
            data["issues"] = [i for i in issues if isinstance(i, str) and i.strip().lower() not in {"none", "n/a", ""}]
            if str(data.get("revision_instructions") or "").strip().lower() in {"none", "n/a"}:
                data["revision_instructions"] = ""
        return data


class Recommendation(BaseModel):
    area: Literal["icp", "scoring", "messaging", "process"]
    change: str
    rationale: str
    expected_impact: str
    confidence: Literal["low", "medium", "high"]


class FeedbackReport(BaseModel):
    summary: str = Field(description="Two-sentence read-out for a sales manager")
    insights: list[str]
    recommendations: list[Recommendation]


def progress_writer():
    """Return a callable that streams a live progress line to the UI (no-op outside a stream)."""
    try:
        from langgraph.config import get_stream_writer

        return get_stream_writer()
    except Exception:  # noqa: BLE001 - not running inside a streamed graph
        return lambda _message: None


def parallel_map(fn, items, workers: int) -> list:
    """Ordered thread-pool map that carries LangGraph's context into each worker,

    so agents can stream progress from inside worker threads.
    """
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(contextvars.copy_context().run, fn, item) for item in items]
        return [future.result() for future in futures]
