"""Feedback agent: turn the run's funnel into recommendations a manager can approve.

It never changes configuration itself; recommendations are returned as
`pending_approval` so a human decides what goes into workflow.json.
"""

from __future__ import annotations

import json
from typing import Any

from ..llm import LLM
from ..state import FeedbackReport, PipelineState, Recommendation, event

AGENT = "Feedback"

SYSTEM = """You are a revenue-operations analyst reviewing one run of a supplier-acquisition campaign for {seller}.
Recommend at most four concrete changes to the ICP, scoring weights, messaging or process.
Tie every recommendation to a number in the data. The sample is small, so set confidence honestly.
Context: low digital maturity is a POSITIVE signal here - suppliers with little online reach gain most from a marketplace.
{caveat}
JSON keys: "summary" (string, two sentences), "insights" (array of up to 3 short strings),
"recommendations" (array of objects with "area" (icp|scoring|messaging|process), "change", "rationale",
"expected_impact", "confidence" (low|medium|high)). Keep every string under 30 words."""


def funnel(state: PipelineState) -> dict[str, Any]:
    scored, drafts = state.get("scored", []), state.get("drafts", [])
    tiers = {t: sum(1 for s in scored if s["tier"] == t) for t in ("qualified", "nurture", "disqualified")}
    return {
        "records_in": len(state.get("leads", [])) + len(state.get("intake_rejects", [])),
        "unique_leads": len(state.get("leads", [])),
        **tiers,
        "drafted": len(drafts),
        "revised": sum(1 for d in drafts if d["revisions"] > 0),
        "approved": sum(1 for d in drafts if d["status"] == "approved"),
        "rejected_by_human": sum(1 for d in drafts if d["status"] == "rejected"),
        "edited_by_human": sum(1 for d in drafts if d.get("edited_by_human")),
        "delivered": state.get("engagement", {}).get("delivered", 0),
    }


def _rule_based(stats: dict[str, Any], engagement: dict[str, Any], state: PipelineState) -> FeedbackReport:
    recs: list[Recommendation] = []
    flags = [flag for item in state.get("scored", []) for flag in item["flags"]]

    if stats["qualified"] == 0:
        recs.append(Recommendation(
            area="icp", change="Lower scoring.qualified_min or widen icp.target_categories",
            rationale=f"0 of {stats['unique_leads']} leads qualified, so no outreach was possible",
            expected_impact="Unblocks the funnel", confidence="medium"))
    if any("tele-calling" in flag for flag in flags):
        recs.append(Recommendation(
            area="process", change="Add a tele-calling / WhatsApp path for leads without a valid email",
            rationale="At least one otherwise-strong lead was held back only because its email is invalid",
            expected_impact="Recovers leads the email channel cannot reach", confidence="high"))
    if stats["rejected_by_human"] or stats["edited_by_human"]:
        recs.append(Recommendation(
            area="messaging", change="Feed human edits and rejections back into the writer prompt",
            rationale=f"Reviewer rejected {stats['rejected_by_human']} and edited {stats['edited_by_human']} of {stats['drafted']} drafts",
            expected_impact="Fewer manual edits per run", confidence="medium"))
    real_engagement = stats["delivered"] and engagement.get("measured")
    if real_engagement and engagement.get("reply_rate", 0) < 0.15:
        recs.append(Recommendation(
            area="process", change="Add a second touch on day 3 for opened-but-not-replied leads",
            rationale=f"Reply rate was {engagement['reply_rate']:.0%} against {engagement['open_rate']:.0%} opens",
            expected_impact="Converts interest that a single email leaves on the table", confidence="low"))

    return FeedbackReport(
        summary=(
            f"{stats['qualified']} of {stats['unique_leads']} leads qualified and "
            f"{stats['approved']} emails were approved. Recommendations below are rule-based."
        ),
        insights=[f"{stats['revised']} draft(s) needed a critic-driven rewrite before reaching the reviewer."],
        recommendations=recs,
    )


def run(state: PipelineState, llm: LLM) -> dict[str, Any]:
    campaign = state["campaign"]
    engagement = state.get("engagement", {})
    stats = funnel(state)
    simulated = engagement.get("source") == "simulated"

    payload = {
        "funnel": stats,
        "engagement": {k: v for k, v in engagement.items() if k != "responses"},
        "per_lead": engagement.get("responses", []),
        "lead_flags": {s["lead"]["company"]: s["flags"] for s in state.get("scored", []) if s["flags"]},
        "scoring_config": campaign["scoring"],
    }
    caveat = (
        "IMPORTANT: open/reply numbers are not real measurements (simulated in a dry run, or not yet known for "
        "emails that were just sent). Do not draw conclusions from them; focus on funnel shape, qualification "
        "flags and reviewer behaviour."
        if not engagement.get("measured")
        else ""
    )
    report = llm.structured(
        FeedbackReport,
        SYSTEM.format(seller=campaign["seller"]["company"], caveat=caveat),
        "Campaign data:\n" + json.dumps(payload, ensure_ascii=False),
        temperature=0.3,
        think=True,
        max_tokens=550,
    )
    engine = "llm"
    if report is None:
        report, engine = _rule_based(stats, engagement, state), "rules"

    feedback = {
        **report.model_dump(),
        "funnel": stats,
        "engine": engine,
        "engagement_is_simulated": simulated,
        "approval_status": "pending_approval",
    }
    message = f"Produced {len(report.recommendations)} recommendation(s) for manager approval (engine: {engine})."
    return {"feedback": feedback, "trace": [event(AGENT, message)]}
