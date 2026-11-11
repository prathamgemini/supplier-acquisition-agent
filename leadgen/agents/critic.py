"""Quality Critic agent: scores each draft and decides whether it goes back to the writer.

Two layers: deterministic guardrails (length, opt-out, placeholders) that an
LLM must never be allowed to wave through, plus an LLM rubric review.
"""

from __future__ import annotations

import re
from typing import Any

from ..llm import LLM
from ..state import Critique, PipelineState, event, parallel_map, progress_writer

AGENT = "Quality Critic"
RUBRIC = ("personalization", "clarity", "value_proposition", "call_to_action", "compliance")
_OPT_OUT_HINTS = ("won't follow up", "will not follow up", "unsubscribe", "opt out", "opt-out")

SYSTEM = """You are a strict reviewer of cold B2B emails sent to Indian SME owners on behalf of {seller}.
Score each rubric item from 1 to 10. Be demanding: 8+ means you would send it unchanged.
- personalization: uses a specific, true fact about this business (not generic praise)
- clarity: short, plain, easy to read on a phone
- value_proposition: explains what the supplier gains, in their own context
- call_to_action: exactly one clear, low-effort ask
- compliance: no invented numbers or claims, respectful, includes an opt-out line
The only facts the writer was allowed to use are in the lead record below.
Flag any claim about the recipient that is not supported by it.
The sender's company, offer and the requested call are given, not invented.

Reply in plain text, in exactly this format and nothing else (no JSON, no markdown):
personalization: <1-10>
clarity: <1-10>
value_proposition: <1-10>
call_to_action: <1-10>
compliance: <1-10>
issues: <up to 3 problems of under 15 words each, separated by " | ", or the word none>
fix: <what the writer should change, under 30 words, or the word none>"""

_NOTHING = {"", "none", "none.", "n/a", "na", "-"}


def parse_critique(text: str | None) -> Critique | None:
    """Read the reviewer's reply. Tolerant by design: it finds each score wherever it appears,

    so a reply that drifts into JSON or adds stray punctuation still parses.
    Returns None only if a score is missing.
    """
    if not text:
        return None
    scores = {}
    for key in RUBRIC:
        match = re.search(key.replace("_", r"[\s_-]*") + r"\W{0,6}(\d{1,2})", text, re.I)
        if not match:
            return None
        scores[key] = int(match.group(1))

    def field(label: str) -> str:
        match = re.search(rf"^\W*{label}\W*:\s*(.*)$", text, re.I | re.M)
        return match.group(1).strip() if match else ""

    issues = [part.strip(" \"'[],") for part in re.split(r"\s*\|\s*|;\s+", field("issues"))]
    issues = [issue for issue in issues if issue.lower() not in _NOTHING]
    fix = field("fix").strip(" \"'")
    return Critique(**scores, issues=issues[:3], revision_instructions="" if fix.lower() in _NOTHING else fix)

USER = """Lead record facts: {facts}

Subject: {subject}

{body}

Review this email."""


def guardrail_issues(draft: dict[str, Any], max_words: int) -> list[str]:
    body, issues = draft["body"].replace("’", "'"), []
    words = len(body.split())
    if words > max_words * 1.25:
        issues.append(f"Body is {words} words; the limit is {max_words}")
    if not any(hint in body.lower() for hint in _OPT_OUT_HINTS):
        issues.append("Missing an opt-out line")
    if re.search(r"\[[^\]]+\]|\{[^}]+\}|<[^>]+>", body + draft["subject"]):
        issues.append("Contains an unfilled placeholder")
    if len(draft["subject"]) > 70:
        issues.append("Subject line is longer than 70 characters")
    return issues


def review(draft: dict[str, Any], facts: str, campaign: dict[str, Any], llm: LLM, final_round: bool) -> dict[str, Any]:
    outreach = campaign["outreach"]
    guard = guardrail_issues(draft, outreach["max_words"])

    # Plain text, parsed in code: this model is far more reliable at "key: value" lines than at JSON.
    critique = parse_critique(
        llm.text(
            SYSTEM.format(seller=campaign["seller"]["company"]),
            USER.format(facts=facts, subject=draft["subject"], body=draft["body"]),
            temperature=0.2,
            max_tokens=220,
        )
    )
    engine = "llm"
    if critique is None:
        engine = "rules"
        base = 9 - min(4, 2 * len(guard))
        critique = Critique(
            personalization=base, clarity=base, value_proposition=base, call_to_action=base,
            compliance=base, issues=[], revision_instructions="",
        )

    scores = {key: max(1, min(10, getattr(critique, key))) for key in RUBRIC}
    quality = round(sum(scores.values()) / len(scores), 1)
    issues = guard + [issue for issue in critique.issues if issue]
    passed = quality >= outreach["quality_threshold"] and scores["compliance"] >= 7 and not guard

    flags = []
    if passed:
        status = "ready"
    elif final_round:
        status = "ready"  # out of revision budget: escalate to the human instead of looping forever
        flags.append("Below quality bar after maximum revisions: review carefully")
    else:
        status = "needs_revision"

    return {
        **draft,
        "status": status,
        "quality": quality,
        "flags": flags,
        "critique": {
            "scores": scores,
            "issues": issues,
            "revision_instructions": critique.revision_instructions or "; ".join(guard),
            "engine": engine,
        },
    }


def _facts(lead: dict[str, Any]) -> str:
    keys = ("company", "contact_name", "title", "city", "state", "category", "products", "employee_count",
            "annual_turnover_lakh", "years_in_business", "gst_registered", "online_presence", "export_markets",
            "recent_signal")
    return "; ".join(f"{k}={lead.get(k)}" for k in keys if lead.get(k) not in (None, "", []))


def run(state: PipelineState, llm: LLM) -> dict[str, Any]:
    campaign = state["campaign"]
    final_round = state.get("revision_round", 0) >= campaign["outreach"]["max_revisions"]
    leads = {item["lead_id"]: item["lead"] for item in state["scored"]}

    emit = progress_writer()

    def maybe_review(draft: dict[str, Any]) -> dict[str, Any]:
        if draft["status"] != "drafted":  # already passed in an earlier round
            return draft
        reviewed = review(draft, _facts(leads[draft["lead_id"]]), campaign, llm, final_round)
        verdict = "send back" if reviewed["status"] == "needs_revision" else "ready"
        emit(f"Critiqued {draft['company']}: {reviewed['quality']}/10 ({verdict})")
        return reviewed

    drafts = parallel_map(maybe_review, state["drafts"], getattr(llm, "concurrency", 4))

    back = [d for d in drafts if d["status"] == "needs_revision"]
    ready = len(drafts) - len(back)
    trace = [event(AGENT, f"Reviewed drafts: {ready} ready for approval, {len(back)} sent back to the writer.")]
    for draft in back:
        why = draft["critique"]["issues"][0] if draft["critique"]["issues"] else "below quality threshold"
        trace.append(event(AGENT, f"{draft['company']} scored {draft['quality']}/10: {why}.", "warn"))
    for draft in drafts:
        for flag in draft["flags"]:
            trace.append(event(AGENT, f"{draft['company']}: {flag}.", "warn"))
    return {"drafts": drafts, "trace": trace}


def route(state: PipelineState) -> str:
    return "revise" if any(d["status"] == "needs_revision" for d in state["drafts"]) else "approve"
