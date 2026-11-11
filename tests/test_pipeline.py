"""Offline tests: the whole graph must run without an API key."""

from __future__ import annotations

import copy

import pytest

from leadgen.agents import critic, enrichment, scoring
from leadgen.config import Settings, load_campaign, validate_campaign
from leadgen.graph import build_graph, new_run, resume_with
from leadgen.llm import LLM
from leadgen.tools.lead_sources import import_csv, parse_csv, template_csv

OFFLINE = Settings(groq_api_key="", groq_model="qwen/qwen3.8-27b", dry_run=True, sendgrid_api_key="", from_email="")


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Each test gets its own empty contact history and do-not-contact list."""
    monkeypatch.setenv("LEADGEN_STORAGE", str(tmp_path))


@pytest.fixture()
def paused_run():
    graph = build_graph(LLM(OFFLINE), OFFLINE)
    state, config = new_run(load_campaign())
    graph.invoke(state, config)
    return graph, config


def test_intake_dedupes_and_tiers_leads(paused_run):
    graph, config = paused_run
    values = graph.get_state(config).values
    assert len(values["leads"]) == 12
    assert [r["reason"] for r in values["intake_rejects"]] == ["Duplicate record"]

    tiers = {s["lead"]["company"]: s["tier"] for s in values["scored"]}
    assert tiers["Vindhya Steel Industries Ltd"] == "disqualified"  # enterprise gate
    assert tiers["Sunrise Specialty Chemicals"] == "nurture"  # invalid email gate
    assert tiers["Mahalaxmi Hardware Traders"] != "qualified"  # no GST, too small
    assert tiers["Shree Balaji Pumps & Motors"] == "qualified"


def test_graph_pauses_for_human_and_never_sends_unapproved(paused_run):
    graph, config = paused_run
    snapshot = graph.get_state(config)
    assert snapshot.next == ("human_review",)
    drafts = snapshot.values["drafts"]
    assert drafts and all(d["status"] == "ready" for d in drafts)
    assert "send_results" not in snapshot.values

    approve, reject = drafts[0]["lead_id"], drafts[1]["lead_id"]
    decisions = {approve: {"decision": "approve", "subject": "Edited subject"}, reject: {"decision": "reject"}}
    graph.invoke(resume_with(decisions), config)
    values = graph.get_state(config).values

    assert [r["lead_id"] for r in values["send_results"]] == [approve]  # undecided drafts default to rejected
    assert values["send_results"][0]["status"] == "simulated"
    assert values["send_results"][0]["subject"] == "Edited subject"
    assert values["engagement"]["source"] == "simulated"
    assert values["feedback"]["approval_status"] == "pending_approval"
    assert values["feedback"]["funnel"]["approved"] == 1


def test_no_approvals_skips_sending(paused_run):
    graph, config = paused_run
    graph.invoke(resume_with({}), config)
    values = graph.get_state(config).values
    assert "send_results" not in values
    assert values["feedback"]["funnel"]["approved"] == 0


def test_critic_guardrails_force_a_revision():
    campaign = load_campaign()
    draft = {"lead_id": "x", "company": "Acme Tools", "subject": "Hello", "status": "drafted", "revisions": 0,
             "history": [], "body": "Hi [Name], we have an amazing offer for you."}
    reviewed = critic.review(draft, "company=Acme Tools", campaign, LLM(OFFLINE), final_round=False)
    assert reviewed["status"] == "needs_revision"
    issues = " ".join(reviewed["critique"]["issues"])
    assert "placeholder" in issues and "opt-out" in issues

    escalated = critic.review(draft, "company=Acme Tools", campaign, LLM(OFFLINE), final_round=True)
    assert escalated["status"] == "ready" and escalated["flags"]


def test_rule_score_rewards_icp_fit():
    campaign = load_campaign()
    base = {"company": "A", "contact_name": "B", "title": "Owner", "email": "a@b.example", "phone": "1",
            "category": "packaging", "employee_count": 60, "annual_turnover_lakh": 900, "gst_registered": True,
            "online_presence": [], "recent_signal": "Installed a new plant and is hiring."}
    good = scoring.rule_breakdown(enrichment.enrich(base), campaign["icp"])
    poor = scoring.rule_breakdown(
        enrichment.enrich({**base, "category": "steel", "employee_count": 5000, "recent_signal": ""}),
        campaign["icp"],
    )
    assert good["category_fit"] == 100 and good["size_fit"] == 100 and good["reachability"] == 100
    assert poor["category_fit"] < good["category_fit"] and poor["size_fit"] < 60 and poor["growth_signals"] is None


def test_campaign_validation_rejects_bad_weights():
    campaign = copy.deepcopy(load_campaign())
    campaign["scoring"]["weights"]["category_fit"] = 0.9
    with pytest.raises(ValueError, match="sum to 1.0"):
        validate_campaign(campaign)


def test_csv_leads_are_parsed_into_the_same_shape():
    text = (
        "company,contact_name,email,employee_count,gst_registered,online_presence\n"
        "Acme Tools,Ravi Kumar,ravi@acme.example,42,yes,website;linkedin\n"
    )
    (lead,) = parse_csv(text)
    assert lead["employee_count"] == 42 and lead["gst_registered"] is True
    assert lead["online_presence"] == ["website", "linkedin"]


class ScriptedLLM:
    """Stands in for Groq: the critic fails every first draft, then passes the rewrite."""

    errors: list[str] = []

    def text(self, system, user, **_):
        if "strict reviewer" in system:
            score = 9 if "Subject: v2" in user else 5
            verdict = "none" if score == 9 else "Too generic"
            fix = "none" if score == 9 else "Use the recent signal"
            return (f"personalization: {score}\nclarity: {score}\nvalue_proposition: {score}\n"
                    f"call_to_action: {score}\ncompliance: 9\nissues: {verdict}\nfix: {fix}")
        company = user.split('"company": "')[1].split('"')[0]
        version = "v2" if "needs changes" in user else "v1"
        filler = "We connect suppliers like you with verified business buyers across India every single week of the year, in your own category and city."
        return f"Subject: {version} for {company}\n\nHi, a note for {company}. {filler}"

    def structured(self, schema, system, user, **_):
        from leadgen.state import Critique, FeedbackReport, LeadAssessment

        if schema is LeadAssessment:
            return LeadAssessment(fit_score=80, rationale="r", pain_points=["p"], personalization_hook="h",
                                  likely_objection="o", recommended_action="outreach_now")
        if schema is Critique:
            score = 9 if "Subject: v2" in user else 5
            return Critique(personalization=score, clarity=score, value_proposition=score, call_to_action=score,
                            compliance=9, issues=[] if score == 9 else ["Too generic"],
                            revision_instructions="" if score == 9 else "Use the recent signal")
        return FeedbackReport(summary="s", insights=[], recommendations=[])


def test_llm_path_runs_the_writer_critic_revision_loop():
    graph = build_graph(ScriptedLLM(), OFFLINE)
    state, config = new_run(load_campaign())
    graph.invoke(state, config)
    values = graph.get_state(config).values

    assert values["revision_round"] == 1
    assert all(s["engine"] == "llm" and s["llm_score"] == 80 for s in values["scored"])
    for draft in values["drafts"]:
        assert draft["status"] == "ready" and draft["revisions"] == 1 and draft["quality"] >= 8
        assert draft["subject"].startswith("v2") and draft["history"][0]["issues"] == ["Too generic"]
        assert draft["body"].endswith("I won't follow up.")  # opt-out appended in code


MESSY_CSV = (
    "Business Name,Owner Name,Mobile No,Industry,Employees,Turnover,GST,Website,Remarks,Lead Source\n"
    'Acme Pumps,Ravi Kumar,9000000000,Industrial Pumps & Machinery,"1,200",15 Cr,24ABCDE1234F1Z5,www.acme.example,'
    "Launched a new range and is hiring dealers,Trade fair\n"
    "Lakshmi Spices,,,,,,,,,Referral\n"
    ",Nobody,,,,,,,,\n"
)


def test_csv_import_accepts_loose_headers_and_blank_cells():
    result = import_csv(MESSY_CSV)
    assert result.usable and result.skipped_rows == 1 and result.extra == ["Lead Source"]
    assert result.mapped["company"] == "Business Name" and result.mapped["phone"] == "Mobile No"
    assert "email" in result.missing

    acme, lakshmi = result.leads
    assert acme["employee_count"] == 1200 and acme["gst_registered"] is True
    assert acme["annual_turnover_lakh"] == 1500  # "15 Cr" converted to lakh
    assert acme["online_presence"] == ["website"] and acme["Lead Source"] == "Trade fair"
    # blank cells are "unknown": the keys are simply absent
    assert lakshmi == {"company": "Lakshmi Spices", "Lead Source": "Referral"}


def test_missing_data_is_not_penalised_but_thin_records_cannot_qualify():
    campaign = load_campaign()
    graph = build_graph(LLM(OFFLINE), OFFLINE)
    state, config = new_run(campaign, parse_csv(MESSY_CSV) + parse_csv(template_csv()))
    graph.invoke(state, config)
    scored = {s["lead"]["company"]: s for s in graph.get_state(config).values["scored"]}

    thin = scored["Lakshmi Spices"]  # the bare row: only a name
    assert thin["tier"] != "qualified" and any("Thin record" in f for f in thin["flags"])
    assert thin["breakdown"]["category_fit"] is None and "GST status" in thin["data_gaps"]

    # category matched loosely ("Industrial Pumps & Machinery" ~ "industrial machinery"), unknown email -> nurture
    acme = scored["Acme Pumps"]
    assert acme["breakdown"]["category_fit"] == 70 and acme["tier"] == "nurture"
    assert not any("GST" in f for f in acme["flags"])


def test_template_rows_run_through_the_pipeline():
    result = import_csv(template_csv())
    assert not result.missing and not result.extra and len(result.leads) == 2
    assert result.leads[1]["online_presence"] == []  # "none" means known to have nothing


def test_emails_always_greet_the_recipient_by_name():
    from leadgen.agents import writer

    assert writer.greeting({"company": "Acme", "contact_name": "Mr. ravi kumar"}) == "Hi Ravi,"
    assert writer.greeting({"company": "Acme Pumps", "contact_name": ""}) == "Hi Acme Pumps team,"
    # whatever salutation the model wrote is replaced; a missing one is added
    assert writer.with_greeting("Hello,\n\nI noticed your new plant.", "Hi Ravi,").startswith("Hi Ravi,\n\nI noticed")
    assert writer.with_greeting("I noticed your new plant.", "Hi Ravi,").startswith("Hi Ravi,\n\nI noticed")

    graph = build_graph(ScriptedLLM(), OFFLINE)
    state, config = new_run(load_campaign())
    graph.invoke(state, config)
    for draft in graph.get_state(config).values["drafts"]:
        assert draft["body"].startswith(f"Hi {draft['contact'].split()[0]},")


class FakeMailer:
    def __init__(self):
        self.outbox = []

    def send(self, to, subject, body):
        self.outbox.append((to, subject))
        return {"status": "sent", "message_id": "<1@test>"}


REAL_LEADS = [
    {"company": "Acme Pumps", "contact_name": "Ravi Kumar", "email": "ravi@acmepumps.in", "title": "Owner",
     "category": "industrial machinery", "employee_count": 80, "gst_registered": True, "online_presence": [],
     "recent_signal": "Commissioned a new assembly line."},
    {"company": "Bharat Packaging", "contact_name": "Asha Rao", "email": "asha@bharatpack.in", "title": "Director",
     "category": "packaging", "employee_count": 60, "gst_registered": True, "online_presence": [],
     "recent_signal": "Installed a new corrugation plant."},
]


def _run_live(monkeypatch, settings, mailer):
    from leadgen.agents import sender

    monkeypatch.setattr(sender, "build_mailer", lambda *_: mailer)
    graph = build_graph(LLM(OFFLINE), settings)
    state, config = new_run(load_campaign(), REAL_LEADS)
    graph.invoke(state, config)
    drafts = graph.get_state(config).values.get("drafts", [])
    graph.invoke(resume_with({d["lead_id"]: {"decision": "approve"} for d in drafts}), config)
    return graph.get_state(config).values


def test_live_send_records_history_and_never_emails_the_same_lead_twice(monkeypatch):
    from leadgen.tools import contacts

    live = Settings(groq_api_key="", groq_model="m", dry_run=False, sendgrid_api_key="", from_email="me@mine.in",
                    smtp_host="smtp.test")
    contacts.add_suppressed("asha@bharatpack.in")
    mailer = FakeMailer()

    first = _run_live(monkeypatch, live, mailer)
    assert [to for to, _ in mailer.outbox] == ["ravi@acmepumps.in"]  # the opted-out lead was never drafted
    assert first["engagement"]["source"] == "live" and first["engagement"]["measured"] is False
    assert "ravi@acmepumps.in" in contacts.load_history()
    tiers = {s["lead"]["company"]: s for s in first["scored"]}
    assert tiers["Bharat Packaging"]["tier"] == "disqualified"

    second = _run_live(monkeypatch, live, mailer)  # same list again
    assert len(mailer.outbox) == 1
    acme = next(s for s in second["scored"] if s["lead"]["company"] == "Acme Pumps")
    assert acme["tier"] == "nurture" and any("Already emailed" in f for f in acme["flags"])


def test_test_mode_redirects_every_email_and_keeps_history_clean(monkeypatch):
    from leadgen.tools import contacts

    test_mode = Settings(groq_api_key="", groq_model="m", dry_run=False, sendgrid_api_key="", from_email="me@mine.in",
                         smtp_host="smtp.test", test_recipient="me@mine.in")
    mailer = FakeMailer()
    values = _run_live(monkeypatch, test_mode, mailer)
    assert {to for to, _ in mailer.outbox} == {"me@mine.in"} and len(mailer.outbox) == 2
    assert all(subject.startswith("[TEST - would go to ") for _, subject in mailer.outbox)
    assert all(r["status"] == "sent_test" for r in values["send_results"])
    assert contacts.load_history() == {}


def test_phone_numbers_never_end_up_in_the_greeting():
    from leadgen.agents import writer

    # "Contact" is a common header for the phone number; the contents decide, not the header.
    text = (
        "Contact,Company,Name,Mail\n"
        "+91 98765 43210,Acme Pumps,Ravi Kumar,ravi@acme.in\n"
        "9876543211,Bharat Packaging,Asha Rao,asha@bharat.in\n"
    )
    result = import_csv(text)
    assert result.mapped["phone"] == "Contact" and result.mapped["contact_name"] == "Name"
    assert result.mapped["email"] == "Mail"
    assert result.leads[0]["contact_name"] == "Ravi Kumar" and result.leads[0]["phone"] == "+91 98765 43210"

    # one stray number in an otherwise good name column is moved, not used as a name
    mixed = import_csv("Company,Owner Name\nAcme,Ravi\nBharat,9876543211\nCoastal,Thomas\nDeccan,Srinivas\n")
    bharat = mixed.leads[1]
    assert "contact_name" not in bharat and bharat["phone"] == "9876543211"

    # and whatever reaches the writer, a number is never used as a name
    assert writer.greeting({"company": "Acme Pumps", "contact_name": "9876543210"}) == "Hi Acme Pumps team,"
    assert writer.greeting({"company": "Acme Pumps", "contact_name": "+91-98765 43210"}) == "Hi Acme Pumps team,"
    assert writer.greeting({"company": "Acme Pumps", "contact_name": "Ravi Kumar"}) == "Hi Ravi,"


def test_critic_reply_is_parsed_even_when_malformed():
    clean = critic.parse_critique(
        "personalization: 7\nclarity: 8\nvalue_proposition: 8\ncall_to_action: 9\ncompliance: 9\n"
        "issues: Generic opener | No specific product named\nfix: Open with the new plant."
    )
    assert clean.personalization == 7 and clean.issues == ["Generic opener", "No specific product named"]
    assert clean.revision_instructions == "Open with the new plant."

    assert critic.parse_critique("personalization: 9\nclarity: 9\nvalue proposition: 9\ncall to action: 9\n"
                                 "compliance: 10\nissues: none\nfix: none").issues == []

    # the broken JSON-ish reply that used to crash the critic
    broken = ('{"errors": ["personalization: 4", "clarity: 7", "value_proposition: 8", "call_to_action: 9", '
              '"compliance: 9", "issues", ":", [""positioned to expand" is an unsupported claim"')
    salvaged = critic.parse_critique(broken)
    assert (salvaged.personalization, salvaged.clarity, salvaged.compliance) == (4, 7, 9)

    proper_json = '{"personalization": 6, "clarity": 7, "value_proposition": 7, "call_to_action": 8, "compliance": 9}'
    assert critic.parse_critique(proper_json).call_to_action == 8
    assert critic.parse_critique("I think this email is fine.") is None
