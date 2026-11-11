"""Streamlit control room for the agentic lead pipeline.

    streamlit run app.py
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from leadgen import LLM, NODES, build_graph, get_settings, load_campaign, new_run, resume_with
from leadgen.tools import contacts
from leadgen.tools.lead_sources import FIELDS, decode, import_csv, load_json, template_csv

st.set_page_config(page_title="Supplier Acquisition Agents", page_icon="🧭", layout="wide")

TIER_BADGE = {"qualified": "🟢 Qualified", "nurture": "🟡 Nurture", "disqualified": "🔴 Disqualified"}
LEVEL_ICON = {"info": "•", "warn": "⚠️", "error": "⛔"}
SS = st.session_state
SS.setdefault("phase", "idle")  # idle -> review -> done
SS.setdefault("data", {})


# --------------------------------------------------------------------------- engine
def start_run(raw_leads: list[dict] | None) -> None:
    settings = get_settings()
    SS.settings = settings
    SS.llm = LLM(settings)
    SS.graph = build_graph(SS.llm, settings)
    state, SS.config = new_run(load_campaign(), raw_leads)
    advance(state, "Agents are working…")


def advance(payload, label: str) -> None:
    """Stream the graph until it finishes or pauses for human review."""
    with st.status(label, expanded=True) as status:
        for mode, chunk in SS.graph.stream(payload, SS.config, stream_mode=["updates", "custom"]):
            if mode == "custom":  # live per-lead progress from inside an agent
                st.caption(f"… {chunk}")
                continue
            for node, update in chunk.items():
                if node == "__interrupt__" or not update:
                    continue
                for item in update.get("trace", []):
                    st.write(f"{LEVEL_ICON[item['level']]} **{item['agent']}** — {item['message']}")
        snapshot = SS.graph.get_state(SS.config)
        SS.data = snapshot.values
        SS.phase = "review" if snapshot.next else "done"
        status.update(
            label="Paused: waiting for your approval" if snapshot.next else "Run complete",
            state="complete",
            expanded=False,
        )
    st.rerun()


def completed_nodes() -> set[str]:
    values, done = SS.data, set()
    if "leads" in values:
        done.add("prospect_search")
    if values.get("leads") and "signals" in values["leads"][0]:
        done.add("enrichment")
    if "scored" in values:
        done.add("qualification")
    if "drafts" in values:
        done |= {"outreach_writer", "quality_critic"}
    if SS.phase == "done" and "drafts" in values:
        done.add("human_review")
    if "send_results" in values:
        done.add("send_outreach")
    if "engagement" in values:
        done.add("response_tracking")
    if "feedback" in values:
        done.add("feedback")
    return done


def graph_dot() -> str:
    done = completed_nodes()
    waiting = "human_review" if SS.phase == "review" else None
    lines = [
        "digraph G { rankdir=LR; bgcolor=transparent;",
        'node [shape=box style="rounded,filled" fontname="Helvetica" fontsize=11 color="#94a3b8" fillcolor="#f1f5f9" fontcolor="#0f172a"];',
        'edge [color="#94a3b8" fontname="Helvetica" fontsize=9 fontcolor="#94a3b8"];',
    ]
    for node_id, label, _ in NODES:
        fill = "#fde68a" if node_id == waiting else "#bbf7d0" if node_id in done else "#f1f5f9"
        lines.append(f'{node_id} [label="{label}" fillcolor="{fill}"];')
    lines += [
        "prospect_search -> enrichment -> qualification;",
        'qualification -> outreach_writer [label="qualified"];',
        'qualification -> feedback [label="none" style=dashed];',
        "outreach_writer -> quality_critic;",
        'quality_critic -> outreach_writer [label="revise" color="#dc2626" fontcolor="#dc2626"];',
        'quality_critic -> human_review [label="ready"];',
        'human_review -> send_outreach [label="approved"];',
        'human_review -> feedback [label="none" style=dashed];',
        "send_outreach -> response_tracking -> feedback;",
        "}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- sidebar
settings = get_settings()
campaign = load_campaign()

with st.sidebar:
    st.header("Run")
    if settings.llm_enabled:
        st.success(f"LLM: Groq · `{settings.groq_model}`")
    else:
        st.warning("Offline mode: no `GROQ_API_KEY` in `.env`. Agents fall back to rules and templates.")
    if settings.can_send_live and settings.test_recipient:
        st.warning(settings.sending_mode)
    elif settings.can_send_live:
        st.error(settings.sending_mode)
    else:
        st.info(settings.sending_mode)

    upload = st.file_uploader(
        "Your leads (CSV, optional)", type="csv",
        help="Only a business-name column is required. Leave this empty to use the built-in sample leads.",
    )
    st.download_button(
        "Download CSV template", template_csv(), file_name="leads_template.csv", mime="text/csv", width="stretch"
    )
    imported = import_csv(decode(upload.getvalue())) if upload else None
    limit = st.number_input(
        "Leads to process", min_value=1, max_value=200, value=4,
        help="Groq's free tier allows about 1,000 output tokens a minute on Qwen, so fewer leads means a faster demo.",
    )
    run_clicked = st.button("▶ Run pipeline", type="primary", width="stretch")

    st.divider()
    st.subheader("Campaign")
    st.caption(f"Selling: **{campaign['seller']['company']}** — {campaign['seller']['description']}")
    st.caption(f"ICP: {campaign['icp']['description']}")
    with st.expander("Scoring weights"):
        st.dataframe(
            pd.DataFrame({"weight": campaign["scoring"]["weights"]}), width="stretch"
        )
        st.caption(
            f"LLM analyst weight {campaign['scoring']['llm_weight']:.0%} · qualified ≥ "
            f"{campaign['scoring']['qualified_min']} · nurture ≥ {campaign['scoring']['nurture_min']}"
        )
    st.caption("Edit `workflow.json` to change any of this.")


# --------------------------------------------------------------------------- header
st.title("Supplier Acquisition Agents")
st.caption(
    "A LangGraph multi-agent pipeline that qualifies SME suppliers for a B2B marketplace, drafts outreach, "
    "critiques its own work, and waits for a human before anything is sent."
)

with st.expander("How to use this app", expanded=SS.phase == "idle" and not upload):
    st.markdown(
        f"""
**1. Choose your leads.** Do nothing to try the built-in sample leads, or upload your own CSV in the sidebar
(in Excel or Google Sheets: *File → Save as / Download → CSV*).

**2. Press Run pipeline.** The agents clean the list, score every lead and draft an email for each qualified one.
Progress appears here line by line. *Leads to process* limits a run to the first N rows.

**3. Review.** The run pauses and waits for you. Open **Outreach review**, edit any email, then choose Approve or
Reject for each and press *Submit decisions*. Nothing is sent without your approval, and with dry run on (the
default) nothing is sent at all.

**4. Read the results.** **Lead qualification** shows why each lead was scored the way it was.
**Results & learning** shows the funnel and suggested improvements.

**Who you are selling as** ({campaign['seller']['company']}), the target customer and the scoring rules are all in
`workflow.json`.
"""
    )
    st.markdown("##### What your CSV needs")
    st.markdown(
        "- **Only the business name is required.** Every other column is optional.\n"
        "- **Column names are flexible.** `Company Name`, `Mobile No`, `Industry`, `Designation`, `Turnover` and "
        "similar headings are recognised automatically. Column order does not matter.\n"
        "- **Blank cells are fine.** A blank means *not known* and is never counted against a lead. The score is "
        "worked out from the information that is there.\n"
        "- **Extra columns are kept** and given to the AI as background.\n"
        "- **The more you fill in, the better the result.** A lead needs an email to be emailed, and at least two of "
        "*category, size, online presence, recent news* to be qualified. Thinner leads are parked as *nurture* "
        "with a note saying what is missing."
    )
    st.dataframe(
        pd.DataFrame(
            [
                {"Column": name, "Type": kind, "What to put in it": help_text, "Also accepted as": ", ".join(aliases[:5])}
                for name, kind, help_text, aliases in FIELDS
            ]
        ),
        hide_index=True,
        width="stretch",
    )

if imported:
    with st.container(border=True):
        st.markdown(f"##### Your file: {len(imported.leads)} lead(s) read from `{upload.name}`")
        if "company" not in imported.mapped:
            st.error(
                "No business-name column found. Rename the column holding the business name to `company` "
                "(or `Company Name`, `Business Name`, `Firm`) and upload again."
            )
        else:
            st.dataframe(
                pd.DataFrame([{"Your column": v, "Understood as": k} for k, v in imported.mapped.items()]),
                hide_index=True, width="stretch",
            )
            if imported.extra:
                st.caption("Not recognised, kept as background for the AI: " + ", ".join(imported.extra))
            if imported.missing:
                st.caption("Not in your file (optional): " + ", ".join(imported.missing))
            if imported.skipped_rows:
                st.warning(f"{imported.skipped_rows} row(s) skipped because the business name was blank.")
            if "email" not in imported.mapped:
                st.warning("No email column: leads can be scored, but no emails will be drafted.")

if run_clicked:  # started here, not in the sidebar, so live progress shows in the main area
    if imported and not imported.usable:
        st.error("This file cannot be used yet. See the message above.")
    else:
        leads = imported.leads if imported else load_json(campaign["lead_source"]["path"])
        start_run(leads[: int(limit)])

values = SS.data
if SS.phase == "idle":
    st.info("Press **Run pipeline** in the sidebar to start.")
elif SS.phase == "review":
    st.warning("The graph is paused at **Human Review**. Open the *Outreach review* tab to approve drafts.")

llm = SS.get("llm")
if llm and llm.calls:
    st.caption(
        f"LLM: {llm.calls} calls to {llm.model} · waited {llm.seconds_waited:.0f}s on Groq rate limits "
        f"({llm.rate_limit_waits} pauses)"
    )
if llm and llm.errors:
    st.error(f"LLM error — affected steps used the rule-based fallback. Last error: {llm.errors[-1]}")

tab_flow, tab_leads, tab_review, tab_results, tab_contacts = st.tabs(
    ["Agent workflow", "Lead qualification", "Outreach review", "Results & learning", "Contacts & opt-outs"]
)

# --------------------------------------------------------------------------- workflow tab
with tab_flow:
    st.graphviz_chart(graph_dot(), width="stretch")
    st.caption("Green = finished · Yellow = waiting for a person · The red edge is the writer ⇄ critic revision loop.")
    left, right = st.columns([1, 1])
    with left:
        st.subheader("Agents")
        for _, label, role in NODES:
            st.markdown(f"**{label}** — {role}")
    with right:
        st.subheader("Decision log")
        if not values.get("trace"):
            st.caption("Nothing yet.")
        for item in values.get("trace", []):
            st.markdown(f"`{item['ts']}` {LEVEL_ICON[item['level']]} **{item['agent']}** — {item['message']}")

# --------------------------------------------------------------------------- leads tab
with tab_leads:
    scored = values.get("scored", [])
    if not scored:
        st.caption("Run the pipeline to see scored leads.")
    else:
        counts = {t: sum(1 for s in scored if s["tier"] == t) for t in TIER_BADGE}
        cols = st.columns(4)
        cols[0].metric("Unique leads", len(scored))
        cols[1].metric("Qualified", counts["qualified"])
        cols[2].metric("Nurture", counts["nurture"])
        cols[3].metric("Disqualified", counts["disqualified"])

        table = pd.DataFrame(
            [
                {
                    "Company": s["lead"]["company"],
                    "City": s["lead"].get("city"),
                    "Category": s["lead"].get("category"),
                    "Score": s["score"],
                    "Rules": s["rule_score"],
                    "Analyst": s["llm_score"],
                    "Tier": TIER_BADGE[s["tier"]],
                    "Why": s["rationale"],
                }
                for s in scored
            ]
        )
        st.dataframe(
            table,
            hide_index=True,
            width="stretch",
            column_config={"Score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f")},
        )

        st.subheader("Why each lead landed where it did")
        for s in scored:
            lead = s["lead"]
            with st.expander(f"{TIER_BADGE[s['tier']]} · {lead['company']} · {s['score']:.0f}"):
                a, b = st.columns([1, 1])
                with a:
                    who = ", ".join(str(x) for x in (lead.get("contact_name"), lead.get("title")) if x)
                    where = ", ".join(str(x) for x in (lead.get("city"), lead.get("state")) if x)
                    st.markdown(f"**{who or 'No contact name'}** · {where or 'location not known'}")
                    st.markdown(f"**Signal:** {lead.get('recent_signal') or '—'}")
                    st.markdown(f"**Analyst rationale** ({s['engine']}): {s['rationale']}")
                    st.markdown("**Pain points:** " + "; ".join(s["pain_points"]))
                    st.markdown(f"**Likely objection:** {s['likely_objection']}")
                    for flag in s["flags"]:
                        st.warning(flag)
                    if s.get("data_gaps"):
                        st.caption("Not known (left out of the score, not penalised): " + ", ".join(s["data_gaps"]))
                with b:
                    known = {k: v for k, v in s["breakdown"].items() if v is not None}
                    st.bar_chart(pd.Series(known, name="Rule score"), horizontal=True, height=220)

# --------------------------------------------------------------------------- review tab
with tab_review:
    drafts = values.get("drafts", [])
    if not drafts:
        st.caption("No drafts yet.")
    elif SS.phase == "review":
        st.markdown(f"**{len(drafts)} draft(s)** passed the critic. Edit if needed, then approve or reject each one.")
        with st.form("approvals"):
            for d in drafts:
                st.markdown(f"#### {d['company']} — {d['contact'] or 'no contact name'}")
                meta = f"Lead score {d['score']:.0f} · Critic {d['quality']}/10 · {d['revisions']} revision(s) · written by {d['engine']}"
                st.caption(meta)
                for flag in d["flags"]:
                    st.warning(flag)
                st.text_input("Subject", d["subject"], key=f"subj_{d['lead_id']}")
                st.text_area("Body", d["body"], height=260, key=f"body_{d['lead_id']}")
                with st.expander("Critic scorecard and revision history"):
                    st.json(d["critique"]["scores"])
                    if d["critique"]["issues"]:
                        st.markdown("**Open issues:** " + "; ".join(d["critique"]["issues"]))
                    for i, old in enumerate(d["history"], 1):
                        st.markdown(f"**Draft {i}** — scored {old['quality']}/10. Issues: {'; '.join(old['issues']) or 'n/a'}")
                        st.code(f"Subject: {old['subject']}\n\n{old['body']}", language=None)
                st.radio("Decision", ["Approve", "Reject"], horizontal=True, key=f"dec_{d['lead_id']}")
                st.divider()
            run_settings = SS.get("settings") or settings
            confirmed = True
            if run_settings.can_send_live:
                (st.warning if run_settings.test_recipient else st.error)(
                    f"**Real emails will be sent.** {run_settings.sending_mode}."
                )
                confirmed = st.checkbox("I have read the approved emails and want them sent")
            submitted = st.form_submit_button("Submit decisions and continue", type="primary")
            if submitted and not confirmed:
                st.error("Tick the confirmation box above to send real emails, or reject the drafts.")
            elif submitted:
                decisions = {
                    d["lead_id"]: {
                        "decision": SS[f"dec_{d['lead_id']}"].lower(),
                        "subject": SS[f"subj_{d['lead_id']}"],
                        "body": SS[f"body_{d['lead_id']}"],
                    }
                    for d in drafts
                }
                advance(resume_with(decisions), "Resuming after human review…")
    else:
        for d in drafts:
            icon = "✅" if d["status"] == "approved" else "🚫"
            edited = " · edited by reviewer" if d.get("edited_by_human") else ""
            with st.expander(f"{icon} {d['company']} — {d['subject']}"):
                st.caption(f"Critic {d['quality']}/10 · {d['revisions']} revision(s){edited}")
                st.text(d["body"])

# --------------------------------------------------------------------------- results tab
with tab_results:
    report = values.get("feedback")
    if not report:
        st.caption("Results appear once the run completes.")
    else:
        funnel = report["funnel"]
        engagement = values.get("engagement", {})
        st.subheader("Funnel")
        steps = ["records_in", "unique_leads", "qualified", "drafted", "approved", "delivered"]
        cols = st.columns(len(steps))
        for col, key in zip(cols, steps):
            col.metric(key.replace("_", " ").title(), funnel[key])

        if engagement.get("delivered"):
            st.subheader("Engagement")
            sends = pd.DataFrame(values.get("send_results", []))
            if engagement["source"] != "simulated":
                st.success(
                    f"{engagement['delivered']} real email(s) were sent. Replies will arrive in the sender's inbox. "
                    "Opens and clicks are only recorded if the SendGrid webhook is set up (see README)."
                )
                st.dataframe(sends.drop(columns=["lead_id"], errors="ignore"), hide_index=True, width="stretch")
        if engagement.get("delivered") and (engagement["source"] == "simulated" or engagement.get("measured")):
            if engagement["source"] == "simulated":
                st.warning("These numbers are **simulated** because this was a dry run. They show the mechanism, not real performance.")
            cols = st.columns(4)
            for col, (key, label) in zip(
                cols, [("open_rate", "Opened"), ("click_rate", "Clicked"), ("reply_rate", "Replied"), ("meeting_rate", "Meeting")]
            ):
                col.metric(label, f"{engagement[key]:.0%}")
            st.dataframe(pd.DataFrame(engagement["responses"]).drop(columns=["lead_id"]), hide_index=True, width="stretch")

        st.subheader("What the Feedback agent recommends")
        st.caption(f"Engine: {report['engine']} · Status: awaiting manager approval — nothing is applied automatically.")
        st.write(report["summary"])
        for insight in report["insights"]:
            st.markdown(f"- {insight}")
        for rec in report["recommendations"]:
            with st.container(border=True):
                st.markdown(f"**{rec['area'].upper()}** · confidence: {rec['confidence']}")
                st.markdown(f"**Change:** {rec['change']}")
                st.markdown(f"**Why:** {rec['rationale']}")
                st.markdown(f"**Expected impact:** {rec['expected_impact']}")

        st.download_button(
            "Download full run (JSON)",
            json.dumps(values, indent=2, ensure_ascii=False, default=str),
            file_name=f"run_{values.get('run_id', 'latest')}.json",
            mime="application/json",
        )

# --------------------------------------------------------------------------- contacts tab
with tab_contacts:
    st.caption(
        "The app's memory between runs. It is checked on every run, so nobody is emailed twice by accident "
        "and nobody who opted out is emailed again. Stored in the `storage/` folder on this computer."
    )
    left, right = st.columns(2)
    with left:
        st.subheader("Do-not-contact list")
        st.caption("When someone replies \"no\" or asks to be removed, add them here.")
        with st.form("add_opt_out", clear_on_submit=True):
            entry = st.text_input("Email address, or a whole domain as @company.com")
            reason = st.text_input("Reason (optional)", "Asked not to be contacted")
            if st.form_submit_button("Add to do-not-contact list"):
                if contacts.add_suppressed(entry, reason):
                    st.success(f"Added {entry.strip().lower()}. They will not be emailed.")
                else:
                    st.error("Enter a full email address, or a domain starting with @.")
        suppressed = contacts.load_suppressed()
        if suppressed:
            st.dataframe(
                pd.DataFrame([{"Entry": k, **v} for k, v in suppressed.items()]), hide_index=True, width="stretch"
            )
            removed = st.selectbox("Remove an entry", ["", *suppressed], format_func=lambda x: x or "Choose…")
            if removed and st.button("Remove from list"):
                contacts.remove_suppressed(removed)
                st.rerun()
        else:
            st.caption("Empty.")
    with right:
        st.subheader("Contact history")
        recontact = campaign["outreach"].get("recontact_after_days", 30)
        st.caption(f"Every real email sent. A lead is not emailed again within {recontact} days.")
        history = contacts.load_history()
        if history:
            st.dataframe(
                pd.DataFrame([{"Email": k, **v} for k, v in history.items()]).sort_values("sent_at", ascending=False),
                hide_index=True, width="stretch",
            )
        else:
            st.caption("No real emails have been sent yet. Dry runs and test-mode sends are not recorded.")
