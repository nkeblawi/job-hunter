# Job Hunter Agent — Nabeel Keblawi

Scans 62 employer career pages daily, scores every listing against your profile
using Claude, and emails you the top N results with fit score, rationale, and a
concrete skills gap for each role.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Fill in config.yaml (see Configuration below)

# 3. Run
python job_hunter.py              # top 10 results (default)
python job_hunter.py -n 5        # top 5
python job_hunter.py -n 20       # top 20
python job_hunter.py --help
```

---

## Configuration (config.yaml)

Four things to fill in before first run:

```yaml
email:
  from:     "your.gmail@gmail.com"       # Gmail you're sending FROM
  username: "your.gmail@gmail.com"       # same address
  password: "xxxx xxxx xxxx xxxx"        # 16-char Gmail App Password (NOT your real password)
                                         # Get it: https://myaccount.google.com/apppasswords

anthropic_api_key: "sk-ant-..."          # console.anthropic.com → API Keys
                                         # Set to "ENV" to use ANTHROPIC_API_KEY env var instead

usajobs_api_key: "YOUR_USAJOBS_API_KEY"  # ← paste your USAJobs key here
usajobs_email:   "your.gmail@gmail.com"  # must match the email you used to request the key
```

Everything else (prompts, org lists) is tunable but works out of the box.

---

## Sources

| Source | Count | Method | Notes |
|---|---|---|---|
| 🌿 Greenhouse API | 14 orgs | Free public GET API | Most reliable; no scraping |
| 🌐 HTML scraping | 48 orgs | Polite scraping (1 req/day) | Static pages work well; Workday/iCIMS may be JS-rendered |
| 🏛️ USAJobs API | Federal listings | Official REST API | Requires free API key |

JS-rendered pages (Workday, iCIMS, Taleo) return empty — the email warnings
section flags these so you can check them manually.

---

## Email Output

Each listing in the email shows:
- **Fit score** (0–100) and **priority** badge (HIGH / MEDIUM / LOW)
- **Rationale** — 2-sentence honest assessment specific to that listing
- **Skills Gap** — amber callout listing only what you're missing for that role
  (suppressed if gap is "None")
- **Flags** — any hard disqualifiers (on-call, relocation, salary, etc.)
- **Apply link**

---

## Org List Summary

**Greenhouse API (14):**
MITRE, Noblis, ICF, Guidehouse, Peraton, Invenergy, Jupiter Intelligence, Verisk,
Accenture Federal Services, Excella, Equinix, Digital Realty, Vantage Data Centers,
Iron Mountain

**HTML — Virginia / County Govt:**
Virginia State Jobs (×2 keyword searches), VITA, Loudoun County, Fairfax County

**HTML — Federal Agencies:**
NOAA (USAJobs search), USGS (USAJobs search)
*(best served by the USAJobs API source — HTML is a fallback)*

**HTML — Weather / Climate / Energy:**
Lynker Technologies, Science & Technology Corp (STC), The Weather Company, DTN,
Wood Mackenzie, EDP Renewables, RE Tech Advisors, Aon (climate risk),
EDF Renewables, ERG, RES, AES Corporation, CleanChoice Energy, Exelon

**HTML — Defense / Federal Contractors:**
Leidos, Booz Allen Hamilton, SAIC, General Dynamics IT, Northrop Grumman,
Maxar/Vantor, ManTech, CGI Federal, SPA, CNA Corporation, CrossCountry Consulting,
Data and Analytic Solutions (DAS), Summit LLC

**HTML — Loudoun County Data Centers:**
QTS, NTT Global, CyrusOne, Oracle Cloud, CoreSite/American Tower,
CloudHQ, Centersquare, STACK Infrastructure, Corscale, EdgeConneX

**HTML — Other:**
ESRI, MWCOG, IntraFi Network, Umbra, Compass Datacenters

---

## Adding More Orgs

**Greenhouse** (best — free API, no scraping):
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

---

## Cost

~$0.10–0.25 per run depending on how many listings are found.
Uses claude-sonnet-4-20250514, max_tokens 1000 per scoring batch.

---

## Tuning

All scoring behavior lives in `config.yaml` under `prompts:` — edit freely:
- `candidate_profile` — your background, hard non-negotiables, preferences
- `score_system` — scoring rules for Greenhouse + USAJobs listings
- `html_extract_system` — extraction + scoring rules for HTML pages

No need to touch `job_hunter.py` for routine tuning.
