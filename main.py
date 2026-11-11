"""Command-line runner.

    python main.py                 # run, then approve drafts interactively
    python main.py --auto-approve  # run end-to-end without prompting (still dry-run)
    python main.py --check-llm     # verify the Groq key and model with one call
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from leadgen import LLM, build_graph, get_settings, load_campaign, new_run, resume_with
from leadgen.tools.lead_sources import decode, import_csv, load_json

ROOT = Path(__file__).resolve().parent


def _stream(graph, payload, config) -> None:
    for mode, chunk in graph.stream(payload, config, stream_mode=["updates", "custom"]):
        if mode == "custom":
            print(f"             ... {chunk}")
            continue
        for node, update in chunk.items():
            if node == "__interrupt__" or not update:
                continue
            for item in update.get("trace", []):
                print(f"  [{item['ts']}] {item['agent']:<17} {item['message']}")


def _collect_decisions(drafts: list[dict], auto: bool) -> dict[str, dict[str, str]]:
    decisions: dict[str, dict[str, str]] = {}
    approve_all = auto
    for draft in drafts:
        if not approve_all:
            print("\n" + "-" * 72)
            print(f"To:      {draft['contact'] or '(no contact name)'} <{draft['to']}>  ({draft['company']})")
            print(f"Quality: {draft['quality']}/10 after {draft['revisions']} revision(s)")
            print(f"Subject: {draft['subject']}\n\n{draft['body']}")
            answer = input("\n[a]pprove  [r]eject  [A]pprove all remaining > ").strip()
            if answer == "A":
                approve_all = True
            elif answer.lower() != "a":
                decisions[draft["lead_id"]] = {"decision": "reject"}
                continue
        decisions[draft["lead_id"]] = {"decision": "approve"}
    return decisions


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Agentic supplier-acquisition pipeline")
    parser.add_argument("--auto-approve", action="store_true", help="approve every draft without prompting")
    parser.add_argument("--leads", help="path to a leads .csv or .json file (default: workflow.json lead_source)")
    parser.add_argument("--limit", type=int, help="only process the first N leads (faster on Groq's free tier)")
    parser.add_argument("--confirm-live", action="store_true", help="allow --auto-approve to send real emails")
    parser.add_argument("--check-llm", action="store_true", help="test the Groq connection and exit")
    args = parser.parse_args()

    settings = get_settings()
    llm = LLM(settings)

    if args.check_llm:
        ok, detail = llm.ping()
        print(("OK: " if ok else "FAILED: ") + detail)
        return 0 if ok else 1

    print(f"Engine : {llm.mode}")
    print(f"Sending: {settings.sending_mode}\n")
    if settings.can_send_live and not settings.test_recipient and args.auto_approve and not args.confirm_live:
        print("Refusing to auto-approve REAL emails. Add --confirm-live if you really mean it.")
        return 1

    graph = build_graph(llm, settings)
    campaign = load_campaign()
    raw_leads = None
    if args.leads and args.leads.lower().endswith(".csv"):
        imported = import_csv(decode(Path(args.leads).read_bytes()))
        if not imported.usable:
            print("This CSV has no business-name column (e.g. 'company' or 'Company Name').")
            return 1
        print(f"CSV columns understood: {imported.mapped}")
        if imported.extra:
            print(f"Kept as background: {imported.extra}")
        raw_leads = imported.leads
    elif args.leads:
        raw_leads = load_json(args.leads)
    if args.limit:
        raw_leads = (raw_leads or load_json(campaign["lead_source"]["path"]))[: args.limit]
    state, config = new_run(campaign, raw_leads)

    _stream(graph, state, config)
    snapshot = graph.get_state(config)
    if snapshot.next:  # paused at the human_review interrupt
        drafts = snapshot.values["drafts"]
        print(f"\nPaused for human review: {len(drafts)} draft(s).")
        _stream(graph, resume_with(_collect_decisions(drafts, args.auto_approve)), config)
        snapshot = graph.get_state(config)

    values = snapshot.values
    report = values.get("feedback", {})
    print("\n" + "=" * 72)
    print("Funnel:", json.dumps(report.get("funnel", {})))
    print("Summary:", report.get("summary", ""))
    for rec in report.get("recommendations", []):
        print(f"  - [{rec['area']}/{rec['confidence']}] {rec['change']}")
    if llm.enabled or llm.calls:
        print(f"\nLLM: {llm.calls} calls, waited {llm.seconds_waited:.0f}s on rate limits ({llm.rate_limit_waits} pauses).")
    if llm.errors:
        print(f"\nNote: {len(llm.errors)} LLM error(s); last: {llm.errors[-1]}")

    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_file.write_text(json.dumps(values, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nFull results: {out_file.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
