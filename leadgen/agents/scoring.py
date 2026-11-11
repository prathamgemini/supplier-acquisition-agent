"""Qualification agent: transparent rule score + LLM analyst judgement -> tier.

The rule score is auditable and cheap; the LLM reads the unstructured context
(recent signal, product mix) that rules miss. The final score blends the two,
and hard gates (enterprise, no GST, unreachable) can only lower a tier.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..llm import LLM
from ..tools import contacts
from ..state import LeadAssessment, PipelineState, event, parallel_map, progress_writer

AGENT = "Qualification"

SYSTEM = """You are a senior sales qualification analyst at {seller}, {description}.
You decide whether an SME supplier is worth a salesperson's time right now.
Judge only from the facts in the lead record - never invent facts.
Ideal customer profile: {icp}
A supplier with real products, growth intent and weak online reach benefits most.
Very large enterprises, unregistered businesses and pure retailers are poor fits.
Keep it short: rationale under 40 words, at most 2 pain points of under 15 words each.
JSON keys, all at the top level: "fit_score" (integer 0-100), "rationale", "pain_points" (array of strings),
"personalization_hook", "likely_objection", "recommended_action" (outreach_now|nurture|disqualify)."""

USER = """Lead record:
{lead}

Rule-based pre-score (0-100 per factor; null = no data for that factor): {breakdown}
Missing fields mean "not known", not "bad". Judge from what is known and say what is missing.

Assess this lead."""


def _band_fit(value: float | None, lo: float, hi: float) -> int | None:
    if value is None:
        return None
    if lo <= value <= hi:
        return 100
    ratio = value / lo if value < lo else hi / value
    return max(0, min(100, round(100 * ratio)))


_FILLER = {"and", "of", "the", "for", "products", "goods", "items", "manufacturer", "manufacturers", "supplier"}


def _category_fit(category: str | None, targets: list[str]) -> int | None:
    """100 = exact target category, 70 = shares a key word with one, 30 = unrelated, None = not known."""
    if not category:
        return None
    category = category.lower().strip()
    targets = [t.lower() for t in targets]
    if category in targets:
        return 100
    words = set(re.findall(r"[a-z]+", category)) - _FILLER
    for target in targets:
        if words & (set(re.findall(r"[a-z]+", target)) - _FILLER):
            return 70
    return 30


def rule_breakdown(lead: dict[str, Any], icp: dict[str, Any]) -> dict[str, int | None]:
    """Score each factor 0-100. A factor with no data is None and is left out of the total."""
    signals = lead["signals"]

    sizes = [
        fit
        for fit in (
            _band_fit(lead.get("employee_count"), icp["employee_count"]["min"], icp["employee_count"]["max"]),
            _band_fit(
                lead.get("annual_turnover_lakh"),
                icp["annual_turnover_lakh"]["min"],
                icp["annual_turnover_lakh"]["max"],
            ),
        )
        if fit is not None
    ]

    # The less visible a supplier is online, the more a marketplace listing is worth to them.
    presence = lead.get("online_presence")
    if presence is None:
        digital_gap = None
    elif not presence:
        digital_gap = 100
    else:
        digital_gap = {"low": 85, "medium": 65, "high": 40}[signals["digital_maturity"]]

    growth = min(100, 30 + 20 * len(signals["growth_keywords"])) if lead.get("recent_signal") else None
    reach = 60 * signals["email_valid"] + 20 * signals["has_phone"] + 20 * signals["is_decision_maker"]

    return {
        "category_fit": _category_fit(lead.get("category"), icp["target_categories"]),
        "size_fit": sum(sizes) // len(sizes) if sizes else None,
        "digital_gap": digital_gap,
        "growth_signals": growth,
        "reachability": reach,
    }


_GAP_LABELS = {
    "category_fit": "business category",
    "size_fit": "company size (employees or turnover)",
    "digital_gap": "online presence",
    "growth_signals": "recent news",
}


def weighted_score(breakdown: dict[str, int | None], weights: dict[str, float]) -> float:
    """Weighted average over the factors that have data."""
    known = {k: w for k, w in weights.items() if breakdown.get(k) is not None}
    return sum(breakdown[k] * w for k, w in known.items()) / sum(known.values())


def hard_gates(
    lead: dict[str, Any], icp: dict[str, Any], breakdown: dict[str, int | None]
) -> tuple[str | None, list[str]]:
    """Return (maximum allowed tier or None, human-readable flags)."""
    flags: list[str] = []
    cap: str | None = None
    employees = lead.get("employee_count") or 0

    if employees > 3 * icp["employee_count"]["max"]:
        flags.append("Enterprise-scale: buys and sells through its own channels")
        cap = "disqualified"
    if lead.get("gst_registered") is False:  # unknown GST status is not held against a lead
        flags.append("Not GST registered: cannot be verified as a seller")
        cap = cap or "nurture"
    known = sum(1 for value in breakdown.values() if value is not None)
    if known < 3:
        flags.append(f"Thin record: only {known} of 5 scoring factors have data, so it cannot be qualified yet")
        cap = cap or "nurture"
    if not lead["signals"]["email_valid"]:
        flags.append("No valid email: route to tele-calling instead of email outreach")
        cap = cap or "nurture"
    return cap, flags


def _heuristic_assessment(lead: dict[str, Any], breakdown: dict[str, int], rule_score: float) -> LeadAssessment:
    signals = lead["signals"]
    breakdown = {k: v for k, v in breakdown.items() if v is not None}
    best = max(breakdown, key=breakdown.get).replace("_", " ")
    worst = min(breakdown, key=breakdown.get).replace("_", " ")

    pains = []
    if signals["digital_maturity"] == "low":
        pains.append("Depends on local agents and referrals; buyers outside the region cannot find them")
    if signals["growth_keywords"]:
        pains.append("Has added capacity or products and needs new buyers to fill it")
    if signals["exports"]:
        pains.append("Export demand is uneven; wants steadier domestic orders")

    return LeadAssessment(
        fit_score=round(rule_score),
        rationale=f"Strongest factor is {best} ({max(breakdown.values())}); weakest is {worst} ({min(breakdown.values())}).",
        pain_points=pains or ["No clear pain point visible in the record"],
        personalization_hook=lead.get("recent_signal") or f"{lead['company']} supplies {lead.get('category') or 'products'}",
        likely_objection=(
            "Already listed on another marketplace"
            if signals["on_other_marketplace"]
            else "Unsure a paid online listing will bring serious buyers"
        ),
        recommended_action="outreach_now" if rule_score >= 65 else "nurture" if rule_score >= 45 else "disqualify",
    )


def _lead_view(lead: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in lead.items() if k not in {"signals", "lead_id", "phone", "email"}}


def _tier(score: float, scoring: dict[str, Any]) -> str:
    if score >= scoring["qualified_min"]:
        return "qualified"
    return "nurture" if score >= scoring["nurture_min"] else "disqualified"


_ORDER = {"qualified": 0, "nurture": 1, "disqualified": 2}


def score_lead(
    lead: dict[str, Any], campaign: dict[str, Any], llm: LLM, memory: dict[str, Any] | None = None
) -> dict[str, Any]:
    memory = memory or {"history": {}, "suppressed": {}}
    opted_out = contacts.is_suppressed(lead.get("email"), memory["suppressed"])
    last_sent = contacts.recently_contacted(
        lead.get("email"), memory["history"], campaign["outreach"].get("recontact_after_days", 30)
    )
    icp, scoring, seller = campaign["icp"], campaign["scoring"], campaign["seller"]
    breakdown = rule_breakdown(lead, icp)
    rule_score = weighted_score(breakdown, scoring["weights"])
    gaps = [label for key, label in _GAP_LABELS.items() if breakdown[key] is None]
    if lead.get("gst_registered") is None:
        gaps.append("GST status")

    # No point spending an LLM call on someone we are not allowed to contact.
    assessment = None if opted_out else llm.structured(
        LeadAssessment,
        SYSTEM.format(seller=seller["company"], description=seller["description"], icp=json.dumps(icp)),
        USER.format(lead=json.dumps(_lead_view(lead), ensure_ascii=False), breakdown=json.dumps(breakdown)),
        temperature=0.3,
        think=True,
        max_tokens=300,
    )
    engine = "llm"
    if assessment is None:
        assessment, engine = _heuristic_assessment(lead, breakdown, rule_score), "heuristic"

    llm_score = max(0, min(100, assessment.fit_score))
    weight = scoring["llm_weight"] if engine == "llm" else 0.0
    final = round((1 - weight) * rule_score + weight * llm_score, 1)

    tier = _tier(final, scoring)
    cap, flags = hard_gates(lead, icp, breakdown)
    if opted_out:
        cap, flags = "disqualified", ["On the do-not-contact list: will never be emailed", *flags]
    elif last_sent:
        flags.append(f"Already emailed on {last_sent}: not contacted again so soon")
        cap = cap or "nurture"
    if cap and _ORDER[cap] > _ORDER[tier]:
        tier = cap
    if engine == "llm" and assessment.recommended_action == "disqualify" and tier == "qualified":
        tier = "nurture"
        flags.append("Analyst advised against outreach despite a passing score")
    if engine == "llm" and abs(llm_score - rule_score) >= 25:
        flags.append(f"Rules ({rule_score:.0f}) and analyst ({llm_score}) disagree: worth a human look")

    return {
        "lead_id": lead["lead_id"],
        "lead": lead,
        "breakdown": breakdown,
        "data_gaps": gaps,
        "rule_score": round(rule_score, 1),
        "llm_score": llm_score if engine == "llm" else None,
        "score": final,
        "tier": tier,
        "flags": flags,
        "engine": engine,
        **assessment.model_dump(exclude={"fit_score"}),
    }


def run(state: PipelineState, llm: LLM) -> dict[str, Any]:
    campaign, leads = state["campaign"], state.get("leads", [])
    emit = progress_writer()
    memory = {"history": contacts.load_history(), "suppressed": contacts.load_suppressed()}

    def work(lead: dict[str, Any]) -> dict[str, Any]:
        item = score_lead(lead, campaign, llm, memory)
        emit(f"Qualified {lead['company']}: {item['score']:.0f} ({item['tier']})")
        return item

    scored = parallel_map(work, leads, getattr(llm, "concurrency", 4))
    scored.sort(key=lambda item: item["score"], reverse=True)

    counts = {tier: sum(1 for s in scored if s["tier"] == tier) for tier in _ORDER}
    engines = {s["engine"] for s in scored}
    trace = [
        event(
            AGENT,
            f"Scored {len(scored)} leads: {counts['qualified']} qualified, "
            f"{counts['nurture']} nurture, {counts['disqualified']} disqualified "
            f"(engine: {', '.join(sorted(engines)) or 'n/a'}).",
        )
    ]
    for item in scored:
        for flag in item["flags"]:
            trace.append(event(AGENT, f"{item['lead']['company']}: {flag}.", "warn"))
    if llm.errors and any(s["engine"] == "heuristic" for s in scored):
        trace.append(event(AGENT, f"LLM unavailable, used rule-based fallback. Last error: {llm.errors[-1]}", "error"))
    return {"scored": scored, "trace": trace}
