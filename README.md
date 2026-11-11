# Supplier Acquisition Agents

An agentic AI app for B2B lead generation, built with **LangGraph** and **Groq (Qwen 3.8 27B)**.

Give it a list of businesses. AI agents clean the list, score each lead and explain why, write a personalised email to each qualified one, critique and rewrite their own drafts, then wait for you to approve before anything is sent. It can run as a safe simulation or send real emails through Gmail or SendGrid.

The built-in demo is a fictional B2B marketplace, *TradeSetu*, recruiting Indian manufacturers and wholesalers as sellers. All sample companies and contacts are fictional.

> **Status: prototype.** It works end to end, but it is a single-user app that runs on one computer.

## Contents

- [How it works](#how-it-works)
- [Install](#install)
- [Add your Groq API key](#add-your-groq-api-key)
- [The dashboard](#the-dashboard)
- [Running a campaign](#running-a-campaign)
- [Using your own leads](#using-your-own-leads)
- [How leads are scored](#how-leads-are-scored)
- [How emails are written and checked](#how-emails-are-written-and-checked)
- [Sending real emails](#sending-real-emails)
- [Contact history and do-not-contact list](#contact-history-and-do-not-contact-list)
- [Changing the campaign (workflow.json)](#changing-the-campaign-workflowjson)
- [Settings (.env)](#settings-env)
- [Command line](#command-line)
- [Good to know](#good-to-know)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)

## How it works

Nine agents run in sequence as one LangGraph workflow. The run pauses at step 6 until a person decides.

| # | Agent | What it does |
| --- | --- | --- |
| 1 | Prospect Search | Loads leads, drops rows with no business name, removes duplicates |
| 2 | Enrichment | Works out signals from each record: valid email, decision-maker title, online presence, growth keywords |
| 3 | Qualification | Scores each lead 0–100 from rules plus an AI analyst, and tiers it as qualified, nurture or disqualified, with a reason |
| 4 | Outreach Writer | Writes a personalised email for each qualified lead, greeting the recipient by name |
| 5 | Quality Critic | Scores each draft and sends weak ones back to the writer (up to 2 rounds) |
| 6 | Human Review | Pauses until you edit, approve or reject each email |
| 7 | Outreach Executor | Sends approved emails (simulated by default) |
| 8 | Response Tracker | Reports engagement (simulated in a dry run) |
| 9 | Feedback | Recommends changes to targeting, scoring or messaging |

```
prospect_search → enrichment → qualification → outreach_writer ⇄ quality_critic
                                                                      ↓
                  feedback ← response_tracking ← send_outreach ← human_review (waits for you)
```

If no lead qualifies, or you approve nothing, the run skips sending and goes straight to feedback.

The app has four modes, shown in the sidebar:

| Mode | When | What happens |
| --- | --- | --- |
| Offline | No Groq key | Rule-based scoring and template emails, no AI |
| Dry run | Default | AI does everything, but no email is sent |
| Test | `TEST_RECIPIENT` is set | Real emails are sent, all to your own address |
| Live | Email configured, no test address | Real emails are sent to the leads |

## Install

You need Python 3.11 or newer ([download](https://www.python.org/downloads/); on Windows tick *"Add python.exe to PATH"*).

**Windows:** double-click `setup.bat`, then `run.bat`. The dashboard opens at `http://localhost:8501`.

**macOS / Linux:**

```bash
bash setup.sh
```

```bash
bash run.sh
```

**Manual install:**

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install -r requirements.txt
```

```bash
.venv\Scripts\python -m streamlit run app.py
```

On macOS / Linux use `.venv/bin/python`, and copy `.env.example` to `.env` yourself.

## Add your Groq API key

1. Create a free key at https://console.groq.com/keys.
2. Open `.env` in the project folder (created by setup) and paste the key after `GROQ_API_KEY=`, with no quotes or spaces.
3. Restart the app. The sidebar shows a green "LLM: Groq" notice.

To check the key from the command line:

```bash
.venv\Scripts\python main.py --check-llm
```

Groq's free tier allows about 1,000 output tokens a minute on this model. The app waits and retries automatically when it hits that limit, so expect roughly a minute per 4 leads. Use *Leads to process* to size a run.

## The dashboard

**Sidebar**

- Status notices: which AI model is connected, and the current sending mode.
- **Your leads (CSV, optional):** upload your own list, or leave empty to use the sample leads.
- **Download CSV template:** a ready-to-fill file with every supported column.
- **Leads to process:** only the first N rows are used.
- **Run pipeline:** starts a run.
- **Campaign:** who you are selling as, the target customer, and the scoring weights.

**Main area**

- **How to use this app:** the steps and CSV rules, on screen.
- **Your file:** after an upload, shows how each of your columns was understood.
- **Agent workflow tab:** the workflow diagram (green = done, yellow = waiting for you), the list of agents, and a decision log of what each agent did.
- **Lead qualification tab:** a table of every lead with score, tier and reason, plus a card per lead with the factor breakdown, pain points, likely objection, flags and missing data.
- **Outreach review tab:** every draft with its critic score and revision history, editable, with Approve / Reject.
- **Results & learning tab:** the funnel, what was sent, engagement, the Feedback agent's recommendations, and a button to download the whole run as JSON.
- **Contacts & opt-outs tab:** the contact history and the do-not-contact list.

## Running a campaign

1. Leave the upload empty to use the sample leads, or upload your own CSV.
2. Set *Leads to process* and press **Run pipeline**. Progress appears line by line.
3. The run pauses at Human Review. Open **Outreach review**, edit any subject or body, choose Approve or Reject for each email, and press *Submit decisions and continue*. Anything not approved is not sent.
4. Read the results in the **Lead qualification** and **Results & learning** tabs.
5. If someone asks not to be contacted, add them in **Contacts & opt-outs**.

## Using your own leads

Upload a CSV in the sidebar. A template is at `data/leads_template.csv` and can be downloaded from the sidebar. From Excel or Google Sheets use *Save as / Download → CSV*.

- **Only the business name is required.** Rows without one are skipped. Everything else is optional.
- **Column names are matched loosely.** `Company Name`, `Firm`, `Contact Person`, `Customer Name`, `Mobile No`, `Industry`, `Designation`, `Staff`, `Turnover`, `GSTIN`, `Website`, `Notes` and similar all work, in any order.
- **Columns are also checked by content.** A column full of phone numbers is treated as the phone column whatever it is called, and likewise for email addresses.
- **Blank cells mean "not known"** and are not counted against a lead.
- **Unrecognised columns are kept** and given to the AI as background.
- **Values are cleaned up:** `1,200` → 1200; `7 Cr` → 700 lakh; a GSTIN counts as GST-registered; a URL in a website column counts as having a website.
- **Duplicates are removed** when two rows share an email or a business name.

| To be… | A lead needs… |
| --- | --- |
| Loaded | a business name |
| Qualified | data for at least 3 of the 5 scoring factors and a score of 65 or more |
| Emailed | a valid email address; otherwise it is kept as *nurture* with a note to call instead |
| Greeted by a person's name | a contact name; otherwise the email opens "Hi *Business Name* team," |
| Personalised well | one sentence of recent news |

| Column | Type | Notes |
| --- | --- | --- |
| `company` | text | Required |
| `contact_name`, `title` | text | Used for the greeting; owner/director titles score higher |
| `email`, `phone` | text | Not sent to the AI model |
| `city`, `state` | text | Personalisation |
| `category` | text | Compared with the target categories in `workflow.json` |
| `products`, `export_markets` | list (`;`) | Personalisation |
| `employee_count` | number | Compared with the target size band |
| `annual_turnover_lakh` | number | In Rs lakh, or with a unit (`80 lakh`, `7 Cr`) |
| `years_in_business` | number | Personalisation |
| `gst_registered` | yes/no | A GSTIN also counts as yes |
| `online_presence` | list (`;`) | e.g. `website;linkedin`; write `none` if they have nothing |
| `recent_signal` | text | One sentence of recent news; used for the growth score and the email opener |

## How leads are scored

Each lead gets a **rule score** from five factors, each 0–100:

| Factor | Weight | How it is scored |
| --- | --- | --- |
| Category fit | 25% | Exact match with a target category 100, shares a key word 70, unrelated 30 |
| Size fit | 20% | Employees and turnover inside the target band 100, falling as they move outside it |
| Digital gap | 20% | No online presence 100, one channel 85, two 65, three or more 40. Less online reach means more to gain |
| Growth signals | 20% | Based on words in the recent news such as "commissioned", "launched", "hiring", "distributor" |
| Reachability | 15% | Valid email 60, phone 20, decision-maker title 20 |

A factor with no data is left out, and the score is the weighted average of the rest.

The **AI analyst** then reads the record and gives its own 0–100 score with a rationale, pain points, a personalisation hook and a likely objection.

**Final score** = 60% rule score + 40% analyst score. Qualified is 65 or above, nurture is 45 to 64, disqualified is below 45.

These rules can only lower a tier:

| Condition | Result |
| --- | --- |
| More than 3 times the maximum employee count | Disqualified (enterprise) |
| On the do-not-contact list | Disqualified |
| Not GST registered | At most nurture |
| No valid email | At most nurture |
| Fewer than 3 of the 5 factors have data | At most nurture |
| Emailed in the last 30 days | At most nurture |
| Analyst advises against outreach | At most nurture |

## How emails are written and checked

- The **writer** gets the lead record and the analyst's notes, and writes a subject and a body of up to 130 words with one call to action. It is told not to invent facts or quote the recipient's own turnover or staff numbers.
- The greeting is always the recipient's first name ("Hi Ravi,"), or "Hi *Business Name* team," when there is no contact name. An opt-out line is always added at the end.
- The **critic** scores each draft 1–10 on personalisation, clarity, value proposition, call to action and compliance, and lists issues.
- Fixed checks also run in code: length, opt-out line present, no unfilled placeholders, subject under 70 characters.
- A draft passes with an average of 8 or more, compliance of 7 or more, and no failed checks. Otherwise it goes back to the writer with instructions, at most twice. After that it is passed to you with a warning.

## Sending real emails

By default the app is in dry run and nothing is sent. To send for real, edit `.env` and restart the app after each change.

### 1. Connect a mailbox

**Gmail (simplest).** Turn on 2-Step Verification in your Google account and create an App password at https://myaccount.google.com/apppasswords. Then set:

```
FROM_EMAIL=you@gmail.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your 16-character app password
```

Other mailboxes work the same way with their own SMTP details (Outlook: `smtp.office365.com`, port 587; Zoho: `smtp.zoho.in`, port 587).

**SendGrid.** Leave `SMTP_HOST` empty and set:

```
FROM_EMAIL=you@yourdomain.com
SENDGRID_API_KEY=your key
```

### 2. Test on yourself

```
ENABLE_DRY_RUN=false
TEST_RECIPIENT=you@gmail.com
```

Every email is now delivered to your own address instead of the lead, with a subject like `[TEST - would go to ravi@acme.in] ...`. Run the pipeline, approve some drafts, tick the confirmation box and check your inbox.

### 3. Go live

1. In `workflow.json`, replace the fictional seller with your company, offer, sender name and call to action.
2. In `.env`, clear the test address: `TEST_RECIPIENT=`
3. Restart the app. The sidebar shows a red **LIVE** notice.

Approved emails now go to the real leads, at most 10 per run. Before sending, the review form asks you to tick a confirmation box. Set `ENABLE_DRY_RUN=true` to go back to safe mode.

### Tracking opens and clicks (optional, SendGrid only)

Run `python webhook_server.py`, make it reachable at a public address, and register `<public-url>/webhook/sendgrid` in SendGrid's Event Webhook settings. Events are saved to `tracking_events.json`.

## Contact history and do-not-contact list

Managed from the **Contacts & opt-outs** tab and stored in the `storage/` folder.

- **Contact history:** every real email is recorded. A lead emailed in the last 30 days is not emailed again, so running the same list twice does not email anyone twice.
- **Do-not-contact list:** add an address, or a whole domain as `@company.com`. Those leads are never drafted or emailed, and the list is checked again at the moment of sending.

Dry runs and test-mode sends are not recorded.

## Changing the campaign (workflow.json)

Everything business-specific is in `workflow.json`. No code changes are needed.

| Setting | What it controls | Default |
| --- | --- | --- |
| `seller.company`, `description` | Who the emails are from | TradeSetu (fictional) |
| `seller.offer`, `cta` | What is offered and the one thing asked for. Write them addressed to the reader ("your catalogue") | |
| `seller.sender_name`, `sender_role` | The signature | |
| `icp.target_categories` | The kinds of business you want | 8 categories |
| `icp.employee_count`, `annual_turnover_lakh` | Target size band | 10–500 staff, Rs 50–10,000 lakh |
| `scoring.weights` | Weight of each factor (must total 1.0) | 25 / 20 / 20 / 20 / 15 |
| `scoring.llm_weight` | Share of the AI analyst in the final score | 0.4 |
| `scoring.qualified_min`, `nurture_min` | Tier thresholds | 65, 45 |
| `outreach.tone` | Writing style | |
| `outreach.max_words` | Email length limit | 130 |
| `outreach.quality_threshold` | Critic pass mark out of 10 | 8 |
| `outreach.max_revisions` | Rewrite rounds | 2 |
| `outreach.max_emails_per_run` | Cap on emails per run | 10 |
| `outreach.recontact_after_days` | Days before a lead can be emailed again | 30 |
| `lead_source.path` | File used when nothing is uploaded | `data/sample_leads.json` |

## Settings (.env)

| Setting | Purpose | Default |
| --- | --- | --- |
| `GROQ_API_KEY` | Your Groq key | |
| `GROQ_MODEL` | The model to use | `qwen/qwen3.8-27b` |
| `GROQ_REASONING` | Thinking depth for the analyst: `none`, `low`, `medium`, `high` | `none` |
| `LLM_CONCURRENCY` | Parallel AI calls. Raise to 6–8 on Groq's paid tier | `2` |
| `ENABLE_DRY_RUN` | `true` = nothing is sent | `true` |
| `FROM_EMAIL` | The address emails come from | |
| `TEST_RECIPIENT` | If set, every email goes to this address instead | |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | Mailbox details for Gmail or any SMTP service | port `587` |
| `SENDGRID_API_KEY` | Used if `SMTP_HOST` is empty | |

`.env` holds your keys and passwords. Do not share or commit it.

## Command line

```bash
.venv\Scripts\python main.py --leads yourfile.csv --limit 5
```

| Option | Effect |
| --- | --- |
| `--leads file.csv` | Use your own CSV or JSON file |
| `--limit N` | Process only the first N leads |
| `--auto-approve` | Approve every draft without asking |
| `--confirm-live` | Needed with `--auto-approve` to send real emails |
| `--check-llm` | Test the Groq key and model |

Without `--auto-approve` it shows each draft and asks you to approve or reject. Each run is saved to `output/run_<timestamp>.json`.

Run the tests with:

```bash
.venv\Scripts\python -m pytest tests -q
```

## Good to know

- **Lead data is sent to Groq.** Every column except `email` and `phone` goes to the AI model.
- **Replies arrive in your inbox.** The app does not read them; add opt-outs to the do-not-contact list yourself.
- **One email per lead.** There are no automatic follow-ups.
- **Engagement in a dry run is simulated** and labelled as such. For real sends, opens and clicks appear only with the SendGrid webhook.
- **A paused run is lost if the app is restarted.** Finish the review before closing it.
- **If the AI returns something unusable,** that step falls back to rules and a red banner says so; the run still completes.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| Sidebar says "Offline mode" | Add `GROQ_API_KEY` to `.env`, save, and restart |
| "LLM error" mentioning rate limit | Lower *Leads to process* or wait a minute |
| A run seems slow | On Groq's free tier it is waiting for the rate limit; progress lines keep appearing |
| "No business-name column found" | Rename that column to `company` |
| Wrong column used for names | Check the "Your file" preview; rename the column to `contact_name` |
| A lead was not emailed | Open it under *Lead qualification*; the flags say why |
| Gmail login fails | Use an App password, not your normal password |
| Still says "Dry run" after setting up email | Set `ENABLE_DRY_RUN=false` and `FROM_EMAIL`, then restart |
| `python` is not recognised | Reinstall Python and tick "Add python.exe to PATH" |

## Project layout

```
setup.bat / run.bat    Install and start (Windows); setup.sh / run.sh for macOS and Linux
app.py                 Dashboard
main.py                Command-line runner
workflow.json          Campaign configuration
.env.example           Template for your settings file (.env)
data/                  Sample leads and the CSV template
leadgen/
  graph.py             The LangGraph workflow
  state.py             Shared state and AI output formats
  llm.py               Groq client and rate-limit handling
  config.py            Settings and campaign validation
  agents/              One file per agent
  tools/               CSV import, email delivery, contact memory
tests/                 Test suite
webhook_server.py      Optional open/click tracking for SendGrid
storage/               Contact history and do-not-contact list (created on first use)
output/                Saved command-line runs (created on first use)
```
