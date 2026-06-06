# Job Hunter Agent — Nabeel Keblawi

An agentic job search tool powered by Claude. The agent autonomously decides which employers
to check, in what order, and stops the moment it finds your target number of HIGH-priority
listings — then emails you the results. You will get an email within 2 to 5 minutes of running
the job_hunter command, which kicks off the agentic workflow.

**DISCLAIMER: This is NOT an auto-apply tool! You can manually apply to the roles listed by this tool.
But if you are looking for something that automatically applies to roles, you're in the wrong place.**

![Sample Email Result](images/email_example.jpg)

---

## Quick Start

```bash
# 1. Activate the virtual environment
source jh/bin/activate

# 2. Copy keys.yaml.example → keys.yaml and fill in your credentials
cp keys.yaml.example keys.yaml

# 3. Run
python job_hunter.py             # find top 5 HIGH-priority results (default)
python job_hunter.py -n 10       # find top 10 before stopping
python job_hunter.py --dry-run   # search and score without sending email
python job_hunter.py --reset     # clear seen log, re-surface old listings
python job_hunter.py --help      # all options
```

---

## Configuration

Credentials live in **`keys.yaml`** (gitignored — never committed). Copy the example to get started:

```bash
cp keys.yaml.example keys.yaml
```

Four values to fill in:

```yaml
email:
  from:     "your.gmail@gmail.com"
  username: "your.gmail@gmail.com"
  password: "xxxx xxxx xxxx xxxx"   # 16-char Gmail App Password
                                     # Get it: https://myaccount.google.com/apppasswords

anthropic_api_key: "sk-ant-..."     # console.anthropic.com → API Keys
                                     # Set to "ENV" to use ANTHROPIC_API_KEY env var instead

usajobs_api_key: "YOUR_KEY"         # optional — email access@usajobs.gov for a free key
usajobs_email:   "your@email.com"
```

Everything else (org lists, scoring prompts, candidate profile) lives in **`config.yaml`**.

---

## How It Works

The agent uses Claude's tool use API in an autonomous loop:

1. **Plans** — calls `list_orgs` to see all configured sources
2. **Fetches** — checks Greenhouse boards (structured API), HTML careers pages, and USAJobs
3. **Scores** — evaluates each listing against the candidate profile in real time
4. **Stops** — the moment it accumulates the target number of HIGH-priority fresh results
5. **Reports** — builds an HTML email and sends it

Unlike a scripted pipeline, the agent decides which orgs to prioritize, skips sources that look
unproductive, and adapts based on what it finds. It never asks for confirmation — just searches,
scores, and reports.

---

## Model Routing (Haiku + Sonnet)

The agent runs on two model tiers, each matched to the kind of work it does:

| Tier | Model | Handles |
|---|---|---|
| **Reasoning** | `claude-sonnet-4-6` | Search orchestration, deciding which orgs to check and when to stop, and **fit-scoring every listing** from all sources |
| **Extraction** | `claude-haiku-4-5` | The mechanical work: parsing raw scraped HTML careers pages into structured listings (title, location, salary, date, URL) |

**Why split it this way?** Greenhouse and USAJobs already return clean structured data, but HTML
pages come back as large blobs of raw page text. Routing that messy extraction to Haiku keeps the
bulky raw text out of the Sonnet context — cutting cost — while **all scoring judgment stays on
Sonnet**, so fit quality is uniform no matter which source a listing came from. If a Haiku
extraction fails, the pipeline falls back to handing the raw text to Sonnet, so a run never breaks.

Both model IDs are configurable under `agent:` in `config.yaml` (`reasoning_model`,
`extraction_model`).

---

## Sources

| Source | Method | Notes |
|---|---|---|
| 🌿 Greenhouse API | Free public GET API | Most reliable; structured data |
| 🌐 HTML scraping | Polite scraping (1 req/day) | Static pages work well; JS-rendered pages flagged |
| 🏛️ USAJobs API | Official REST API | Requires free API key |

JS-rendered pages (Workday, iCIMS, Taleo) return little text — the email warnings section
flags these so you can check them manually.

---

## Email Output

Each listing shows:
- **Fit score** (0–100) and **priority** badge (HIGH / MEDIUM / LOW)
- **Rationale** — 2-sentence honest assessment specific to that listing
- **Skills Gap** — amber callout for what you're missing (suppressed if none)
- **Flags** — hard disqualifiers (on-call, relocation required, salary, etc.)
- **Apply link**

---

## Adding Orgs

**Greenhouse:**
```yaml
greenhouse_orgs:
  - name: "Company Name"
    board_token: "slug"   # verify: https://boards.greenhouse.io/SLUG
```

**HTML fallback:**
```yaml
html_orgs:
  - name: "Org Name"
    careers_url: "https://careers.example.com/jobs"
```

**USAJobs** is configured via `usajobs_searches` keywords in `config.yaml`.

---

## Cost

~$0.15–0.40 per run depending on how many orgs are checked and listings scored.
Uses tiered model routing — `claude-sonnet-4-6` for reasoning and scoring,
`claude-haiku-4-5` for HTML extraction (see [Model Routing](#model-routing-haiku--sonnet)).
Runs also stop early once the HIGH-priority target is hit, which limits cost on
days with many matching listings.

---

## Tuning

All scoring behavior lives in `config.yaml` under `prompts:` — edit freely:
- `candidate_profile` — your background, hard non-negotiables, location rules, preferences
- `score_system` — scoring rules for Greenhouse and USAJobs listings

No need to touch `job_hunter.py` for routine tuning.
