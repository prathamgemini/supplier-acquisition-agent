"""Enrichment agent: derive decision-ready signals from each raw lead.

Everything here is computed from the lead record itself. This is the place to
plug in a paid data provider (GST lookup, company registry, web research).
"""

from __future__ import annotations

from typing import Any

from ..state import PipelineState, event
from .prospect import EMAIL_RE

AGENT = "Enrichment"

DECISION_MAKER_TITLES = ("owner", "proprietor", "founder", "partner", "director", "managing", "ceo", "secretary")
GROWTH_KEYWORDS = (
    "commissioned", "installed", "launched", "opened", "expanded", "expansion", "capacity",
    "appointing", "hiring", "distributor", "dealer", "licence", "license", "certified",
    "bulk", "institutional", "export", "new ",
)


def enrich(lead: dict[str, Any]) -> dict[str, Any]:
    presence = lead.get("online_presence")  # None = not known, [] = known to have nothing
    employees = lead.get("employee_count")
    signal_text = (lead.get("recent_signal") or "").lower()
    title = (lead.get("title") or "").lower()

    if employees is None:
        size_band = "unknown"
    elif employees < 10:
        size_band = "micro"
    elif employees < 50:
        size_band = "small"
    elif employees < 250:
        size_band = "medium"
    else:
        size_band = "large"

    signals = {
        "email_valid": bool(EMAIL_RE.match(lead.get("email") or "")),
        "has_phone": bool(lead.get("phone")),
        "is_decision_maker": any(word in title for word in DECISION_MAKER_TITLES),
        "digital_maturity": (
            "unknown" if presence is None else "low" if len(presence) <= 1 else "medium" if len(presence) == 2 else "high"
        ),
        "on_other_marketplace": "other_marketplace" in (presence or []),
        "size_band": size_band,
        "growth_keywords": sorted({word.strip() for word in GROWTH_KEYWORDS if word in signal_text}),
        "exports": bool(lead.get("export_markets")),
    }
    return {**lead, "signals": signals}


def run(state: PipelineState) -> dict[str, Any]:
    leads = [enrich(lead) for lead in state.get("leads", [])]
    bad_email = [lead["company"] for lead in leads if not lead["signals"]["email_valid"]]
    low_digital = sum(1 for lead in leads if lead["signals"]["digital_maturity"] == "low")

    trace = [event(AGENT, f"Enriched {len(leads)} leads; {low_digital} have little or no online presence.")]
    if bad_email:
        trace.append(event(AGENT, f"Invalid email for: {', '.join(bad_email)}. They cannot be emailed.", "warn"))
    return {"leads": leads, "trace": trace}
