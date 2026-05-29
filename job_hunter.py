#!/usr/bin/env python3
"""
Job Hunter Agent — using Claude
Claude autonomously decides which orgs to check, extracts and scores listings,
and stops when it finds the target number of HIGH-priority results.

Usage:
  python job_hunter.py             # find top 5 HIGH-priority results (default)
  python job_hunter.py -n 10       # find top 10 HIGH-priority results
  python job_hunter.py --reset     # clear seen log before running
  python job_hunter.py --dry-run   # search and score but don't send email
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
import requests
import yaml
from bs4 import BeautifulSoup

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG  — points at the existing job-hunter config so orgs stay in one place
# ══════════════════════════════════════════════════════════════════════════════

_HERE = Path(__file__).parent
CONFIG_PATH = _HERE / "config.yaml"
KEYS_PATH = _HERE / "keys.yaml"
SEEN_LOG = _HERE / "seen_jobs.json"
MAX_ITERS = 60  # safety cap on agent loop iterations


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    try:
        with open(KEYS_PATH) as f:
            keys = yaml.safe_load(f)
        if keys:
            cfg.update(keys)
    except FileNotFoundError:
        print(f"⚠️  keys.yaml not found at {KEYS_PATH} — using ENV vars only.")
    return cfg


# ══════════════════════════════════════════════════════════════════════════════
# SEEN-JOBS LOG
# ══════════════════════════════════════════════════════════════════════════════


def seen_key(job: dict) -> str:
    return f"{job.get('org','').lower().strip()}|{job.get('title','').lower().strip()}"


def load_seen() -> dict:
    try:
        with open(SEEN_LOG) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen: dict, new_jobs: list[dict], run_date: str):
    for job in new_jobs:
        k = seen_key(job)
        if k not in seen:
            seen[k] = {
                "first_seen": run_date,
                "title": job.get("title", ""),
                "org": job.get("org", ""),
            }
    with open(SEEN_LOG, "w") as f:
        json.dump(seen, f, indent=2)
    print(f"  Seen-jobs log updated → {len(seen)} total entries")


# ══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════════════════════


def is_recent(date_str: str, days: int = 3) -> bool:
    if not date_str or str(date_str).lower() == "unknown":
        return False
    try:
        dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        return dt >= datetime.now(timezone.utc) - timedelta(days=days)
    except Exception:
        return False


BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

SOURCE_BADGES = {
    "greenhouse": "🌿 Greenhouse",
    "html": "🌐 Web",
    "usajobs": "🏛️ USAJobs",
}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL IMPLEMENTATIONS  — called by the agent loop when Claude invokes a tool
# ══════════════════════════════════════════════════════════════════════════════


def tool_list_orgs(cfg: dict) -> dict:
    """Return all configured orgs so the agent can plan its search strategy."""
    greenhouse = [
        {
            "name": o["name"],
            "type": "greenhouse",
            "board_token": o["board_token"],
        }
        for o in cfg.get("greenhouse_orgs", [])
    ]
    html = [
        {
            "name": o["name"],
            "type": "html",
            "urls": o.get("careers_urls") or [o.get("careers_url", "")],
            "skip": o.get("skip", False),
        }
        for o in cfg.get("html_orgs", [])
    ]
    api_key = cfg.get("usajobs_api_key", "")
    usajobs_ready = bool(api_key and api_key != "YOUR_USAJOBS_API_KEY")
    return {
        "greenhouse_orgs": greenhouse,
        "html_orgs": html,
        "usajobs_available": usajobs_ready,
        "usajobs_keywords": (
            [s.get("Keyword") for s in cfg.get("usajobs_searches", [])]
            if usajobs_ready
            else []
        ),
        "total_orgs": len(greenhouse) + len(html),
    }


def tool_fetch_greenhouse(
    board_token: str, org_name: str, seen: dict, days: int = 3
) -> dict:
    """Fetch recent job listings from a Greenhouse ATS board."""
    TITLE_KW = [
        "data",
        "analyst",
        "engineer",
        "scientist",
        "analytics",
        "machine learning",
        "ml",
        "ai",
        "intelligence",
        "python",
        "cloud",
        "etl",
        "pipeline",
        "meteorolog",
        "climate",
        "weather",
        "atmospheric",
        "research",
        "snowflake",
        "sql",
    ]
    try:
        resp = requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs",
            params={"content": "true"},
            timeout=15,
        )
        if resp.status_code == 404:
            return {"error": f"Board token '{board_token}' not found", "org": org_name}
        resp.raise_for_status()
        all_jobs = resp.json().get("jobs", [])

        listings = []
        for job in all_jobs:
            title = job.get("title", "").lower()
            if not any(kw in title for kw in TITLE_KW):
                continue
            date = job.get("created_at") or job.get("updated_at") or ""
            if date and not is_recent(date, days):
                continue
            listing = {
                "org": org_name,
                "title": job.get("title", ""),
                "location": job.get("location", {}).get("name", "Unknown"),
                "salary": "Not listed",
                "posted": date,
                "url": job.get("absolute_url", ""),
                "source": "greenhouse",
                "already_seen": seen_key(
                    {"org": org_name, "title": job.get("title", "")}
                )
                in seen,
            }
            listings.append(listing)

        return {
            "org": org_name,
            "listings": listings,
            "total_on_board": len(all_jobs),
            "after_filters": len(listings),
        }
    except Exception as e:
        return {"error": str(e), "org": org_name}


def tool_fetch_html(org_name: str, urls: list[str], seen: dict) -> dict:
    """Fetch and parse HTML careers pages, returning raw text for Claude to analyze."""
    JOB_SIGNALS = [
        "apply",
        "job",
        "position",
        "role",
        "engineer",
        "analyst",
        "scientist",
        "manager",
        "director",
        "remote",
        "salary",
        "full-time",
        "experience",
        "required",
        "qualifications",
        "responsibilities",
        "opening",
        "hiring",
    ]
    combined_text = ""
    for url in urls:
        try:
            resp = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            page_text = soup.get_text(separator="\n", strip=True)
            if len(page_text) < 500:
                combined_text += f"\n[JS-rendered — little text from {url}]"
            else:
                combined_text += f"\n--- URL: {url} ---\n{page_text[:4000]}"
        except Exception as e:
            combined_text += f"\n[Error fetching {url}: {e}]"
        time.sleep(0.5)

    if not combined_text.strip():
        return {"error": "No text fetched", "org": org_name}

    signal_hits = sum(1 for w in JOB_SIGNALS if w in combined_text.lower())
    if signal_hits < 2:
        return {
            "org": org_name,
            "note": f"Page looks like nav/boilerplate ({signal_hits} job signals) — likely no listings",
            "raw_text": None,
        }

    # Mark any titles in the text that are already seen (best-effort hint)
    seen_hint = [k.split("|")[1] for k in seen if k.startswith(org_name.lower())]
    return {
        "org": org_name,
        "raw_text": combined_text[:10000],
        "first_url": urls[0] if urls else "",
        "already_seen_titles": seen_hint[:10],  # hint so agent can skip duplicates
    }


def tool_fetch_usajobs(keyword: str, cfg: dict, seen: dict, days: int = 3) -> dict:
    """Search USAJobs for federal positions matching a keyword."""
    api_key = cfg.get("usajobs_api_key", "")
    email = cfg.get("usajobs_email", "")
    if not api_key or api_key == "YOUR_USAJOBS_API_KEY":
        return {"error": "USAJobs API key not configured — skipping"}
    try:
        resp = requests.get(
            "https://data.usajobs.gov/api/Search",
            headers={
                "Host": "data.usajobs.gov",
                "User-Agent": email,
                "Authorization-Key": api_key,
            },
            params={
                "Keyword": keyword,
                "DatePosted": days,
                "ResultsPerPage": 25,
                "Fields": "min",
            },
            timeout=15,
        )
        resp.raise_for_status()
        listings = []
        for item in resp.json().get("SearchResult", {}).get("SearchResultItems", []):
            d = item.get("MatchedObjectDescriptor", {})
            rem = d.get("PositionRemuneration", [{}])
            locs = d.get("PositionLocation", [{}])
            s_min, s_max = (
                (rem[0].get("MinimumRange", ""), rem[0].get("MaximumRange", ""))
                if rem
                else ("", "")
            )
            org_name = d.get(
                "OrganizationName", d.get("DepartmentName", "Federal Agency")
            )
            title = d.get("PositionTitle", "")
            listing = {
                "org": org_name,
                "title": title,
                "location": (
                    locs[0].get("LocationName", "Unknown") if locs else "Unknown"
                ),
                "salary": f"${s_min}–${s_max}" if s_min and s_max else "Not listed",
                "posted": d.get("PublicationStartDate", ""),
                "url": d.get("PositionURI", ""),
                "source": "usajobs",
                "already_seen": seen_key({"org": org_name, "title": title}) in seen,
            }
            listings.append(listing)
        return {"keyword": keyword, "listings": listings}
    except Exception as e:
        return {"error": str(e), "keyword": keyword}


def tool_send_report(
    jobs: list[dict], summary: str, cfg: dict, run_date: str, dry_run: bool
) -> dict:
    """Build an HTML report and email it (or print it in dry-run mode)."""
    rows = ""
    for i, job in enumerate(jobs, 1):
        url = job.get("url", "")
        link = f'<a href="{url}" style="color:#2980b9">Apply →</a>' if url else "—"
        score = job.get("fit_score", 0)
        sc = "#27ae60" if score >= 75 else "#e67e22" if score >= 50 else "#c0392b"
        pri = job.get("priority", "?")
        pc = {"HIGH": "#27ae60", "MEDIUM": "#e67e22"}.get(pri, "#95a5a6")
        badge = SOURCE_BADGES.get(job.get("source", "html"), "🌐 Web")
        gap = job.get("skills_gap", "") or ""
        gap_html = ""
        if gap.strip() and gap.strip().lower() not in ("none", "—", "-", ""):
            gap_html = (
                f'<div style="margin-top:7px;padding:5px 8px;background:#fff8e1;'
                f'border-left:3px solid #f39c12;border-radius:3px;font-size:11px;color:#7d6608">'
                f"<strong>Gap:</strong> {gap}</div>"
            )
        flags_html = (
            f'<br><span style="color:#c0392b;font-size:11px">⚑ {job["flags"]}</span>'
            if job.get("flags")
            else ""
        )
        rows += f"""
        <tr style="background:{'#f9fbff' if i%2 else '#fff'}">
          <td style="padding:11px;font-weight:bold;color:#2c3e50">#{i}</td>
          <td style="padding:11px">
            <div style="font-weight:bold">{job.get('title','')}</div>
            <div style="color:#666;font-size:12px">{job.get('org','')}</div>
            <div style="font-size:11px;color:#aaa;margin-top:2px">{badge}</div>
          </td>
          <td style="padding:11px;font-size:13px">{job.get('location','')}</td>
          <td style="padding:11px;font-size:13px;white-space:nowrap">{job.get('salary','—')}</td>
          <td style="padding:11px;text-align:center">
            <span style="font-size:20px;font-weight:bold;color:{sc}">{score}</span>
          </td>
          <td style="padding:11px;text-align:center">
            <span style="background:{pc};color:white;padding:3px 9px;border-radius:12px;font-size:12px">{pri}</span>
          </td>
          <td style="padding:11px;font-size:12px;white-space:nowrap">{str(job.get('posted',''))[:10] or '?'}</td>
          <td style="padding:11px;font-size:12px;color:#444">{job.get('rationale','')}{flags_html}{gap_html}</td>
          <td style="padding:11px">{link}</td>
        </tr>"""

    table = (
        f"""
    <table style="width:100%;border-collapse:collapse">
      <thead style="background:#2c3e50;color:white">
        <tr>
          <th style="padding:11px;text-align:left">#</th>
          <th style="padding:11px;text-align:left">Role / Org</th>
          <th style="padding:11px;text-align:left">Location</th>
          <th style="padding:11px;text-align:left">Salary</th>
          <th style="padding:11px;text-align:center">Fit</th>
          <th style="padding:11px;text-align:center">Priority</th>
          <th style="padding:11px;text-align:left">Posted</th>
          <th style="padding:11px;text-align:left">Rationale / Skills Gap</th>
          <th style="padding:11px;text-align:left">Apply</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""
        if jobs
        else """
    <div style="padding:30px;text-align:center;color:#888;background:#f9f9f9;border-radius:8px">
      No matching listings found today. Try again tomorrow or add more orgs to config.yaml.
    </div>"""
    )

    html = f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
                       max-width:1050px;margin:auto;padding:24px;background:#f5f7fa">
      <div style="background:white;border-radius:8px;padding:24px;
                  box-shadow:0 1px 3px rgba(0,0,0,.1)">
        <h2 style="color:#2c3e50;margin-top:0">🤖 Agentic Job Hunt — {run_date}</h2>
        <p style="color:#555;font-style:italic;margin-bottom:16px;padding:10px 14px;
                  background:#f0f4ff;border-left:4px solid #4a6fa5;border-radius:4px">
          Agent summary: {summary}
        </p>
        <p style="color:#666;margin-bottom:20px">
          Filters: remote/hybrid Loudoun Co. VA · $120K+ · no on-call · no sales/marketing
        </p>
        {table}
      </div>
      <p style="color:#bbb;font-size:11px;text-align:center;margin-top:16px">
        Job Hunter Agent · {run_date}
      </p>
    </body></html>"""

    if dry_run:
        print("\n" + "─" * 60)
        print("DRY RUN — email not sent. Jobs the agent selected:")
        for i, j in enumerate(jobs, 1):
            print(
                f"  {i}. [{j.get('priority','?')} / {j.get('fit_score',0)}] "
                f"{j.get('title','')} @ {j.get('org','')} — {j.get('location','')}"
            )
        print("─" * 60)
        return {"status": "dry_run", "job_count": len(jobs)}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🤖 Agentic Job Hunt — {run_date}"
    msg["From"] = cfg["email"]["from"]
    msg["To"] = cfg["email"]["to"]
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL(cfg["email"]["smtp_host"], cfg["email"]["smtp_port"]) as s:
        s.login(cfg["email"]["username"], cfg["email"]["password"])
        s.sendmail(cfg["email"]["from"], cfg["email"]["to"], msg.as_string())
    return {"status": "sent", "recipient": cfg["email"]["to"], "job_count": len(jobs)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL SCHEMAS  — passed to Claude so it knows what tools are available
# ══════════════════════════════════════════════════════════════════════════════

TOOLS = [
    {
        "name": "list_orgs",
        "description": (
            "List all configured employers with their type (greenhouse, html, or usajobs). "
            "Call this first to plan your search strategy."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fetch_greenhouse_jobs",
        "description": (
            "Fetch recent job listings from a Greenhouse ATS board. "
            "Returns structured listings with title, location, URL, and posted date. "
            "Listings already emailed to the user are flagged with already_seen=true — skip those."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "board_token": {
                    "type": "string",
                    "description": "Greenhouse board token from list_orgs",
                },
                "org_name": {
                    "type": "string",
                    "description": "Human-readable org name",
                },
            },
            "required": ["board_token", "org_name"],
        },
    },
    {
        "name": "fetch_html_jobs",
        "description": (
            "Fetch HTML careers pages for an org and return raw page text. "
            "You must extract and score the job listings yourself from the raw_text field. "
            "already_seen_titles lists titles previously emailed — skip those when extracting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "org_name": {
                    "type": "string",
                    "description": "Human-readable org name",
                },
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Careers page URLs (from list_orgs)",
                },
            },
            "required": ["org_name", "urls"],
        },
    },
    {
        "name": "fetch_usajobs",
        "description": (
            "Search USAJobs for federal positions matching a keyword. "
            "Call once per keyword. Returns structured listings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Search keyword e.g. 'data engineer'",
                },
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "send_report",
        "description": (
            "Build an HTML email and send the final job report. "
            "Call when you have reached the HIGH-priority target or exhausted all sources. "
            "Pass only non-already_seen listings, scored and ranked best-first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "jobs": {
                    "type": "array",
                    "description": "Scored, ranked job listings to include (best first)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "org": {"type": "string"},
                            "title": {"type": "string"},
                            "location": {"type": "string"},
                            "salary": {"type": "string"},
                            "posted": {"type": "string"},
                            "url": {"type": "string"},
                            "source": {
                                "type": "string",
                                "enum": ["greenhouse", "html", "usajobs"],
                            },
                            "fit_score": {
                                "type": "number",
                                "description": "0-100 fit score",
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["HIGH", "MEDIUM", "LOW"],
                            },
                            "rationale": {
                                "type": "string",
                                "description": "1-2 sentence fit explanation",
                            },
                            "skills_gap": {
                                "type": "string",
                                "description": "Key gaps, or empty string",
                            },
                            "flags": {
                                "type": "string",
                                "description": "Disqualifiers, or empty string",
                            },
                        },
                        "required": [
                            "org",
                            "title",
                            "location",
                            "url",
                            "source",
                            "fit_score",
                            "priority",
                            "rationale",
                        ],
                    },
                },
                "summary": {
                    "type": "string",
                    "description": "2-3 sentence summary of search strategy, orgs checked, and what you found",
                },
            },
            "required": ["jobs", "summary"],
        },
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# AGENT LOOP
# ══════════════════════════════════════════════════════════════════════════════


def run_agent(cfg: dict, target_n: int, seen: dict, run_date: str, dry_run: bool):
    client = anthropic.Anthropic()
    prompts = cfg.get("prompts", {})
    profile = prompts.get("candidate_profile", "")
    scoring = prompts.get("score_system", "")

    system_prompt = f"""You are an autonomous job hunting agent. Your mission: find {target_n} HIGH-priority \
job listings for the candidate below, then send a report.

CANDIDATE PROFILE:
{profile}

SCORING RULES:
{scoring}

HOW TO WORK:
1. Call list_orgs to see all configured sources.
2. Choose an efficient search order — start with Greenhouse orgs (structured data, fast), \
then HTML orgs, then USAJobs keywords.
3. For each Greenhouse or USAJobs result: score every listing you receive directly.
4. For each HTML result: extract listings from raw_text, then score them.
5. Skip any listing where already_seen=true or title appears in already_seen_titles.
6. Track your HIGH-priority count. Once you hit {target_n} fresh HIGH results, \
call send_report immediately — don't keep searching.
7. If you exhaust all sources before reaching {target_n}, call send_report with whatever you found.

SCORING SCALE: fit_score 0-100. HIGH ≥ 70, MEDIUM 40-69, LOW < 40. \
Violate any hard non-negotiable → fit_score ≤ 15, priority = LOW.

Be decisive. Never ask for confirmation. Search → score → report."""

    messages = [
        {
            "role": "user",
            "content": (
                f"Start the job hunt. Find {target_n} HIGH-priority listings and send the report. "
                f"Today is {run_date}."
            ),
        }
    ]

    print(f"\n🤖 Agent starting — target: {target_n} HIGH-priority results\n{'─'*50}")

    report_sent = False
    final_jobs = []

    for iteration in range(MAX_ITERS):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        # Log any text blocks the agent emits (its reasoning)
        for block in response.content:
            if block.type == "text" and block.text.strip():
                # Indent and truncate for readability
                preview = block.text.strip()[:300].replace("\n", "\n  ")
                print(f"\n  💭 {preview}{'…' if len(block.text) > 300 else ''}")

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            print("\n✅ Agent finished (end_turn).")
            break

        if response.stop_reason != "tool_use":
            print(f"\n⚠️  Unexpected stop_reason: {response.stop_reason}")
            break

        # Execute each tool call
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            name = block.name
            inp = block.input
            label = ", ".join(f"{k}={repr(v)[:50]}" for k, v in inp.items())
            print(f"\n  🔧 {name}({label})")

            if name == "list_orgs":
                result = tool_list_orgs(cfg)
                print(
                    f"     → {result['total_orgs']} orgs ({len(result['greenhouse_orgs'])} Greenhouse, "
                    f"{len(result['html_orgs'])} HTML, USAJobs={'yes' if result['usajobs_available'] else 'no'})"
                )

            elif name == "fetch_greenhouse_jobs":
                result = tool_fetch_greenhouse(
                    inp["board_token"], inp["org_name"], seen
                )
                n_new = sum(
                    1 for l in result.get("listings", []) if not l.get("already_seen")
                )
                print(
                    f"     → {len(result.get('listings', []))} listing(s) ({n_new} new)"
                )

            elif name == "fetch_html_jobs":
                result = tool_fetch_html(inp["org_name"], inp["urls"], seen)
                if result.get("raw_text"):
                    print(f"     → {len(result['raw_text'])} chars fetched")
                else:
                    print(
                        f"     → {result.get('note', result.get('error', 'no content'))}"
                    )

            elif name == "fetch_usajobs":
                result = tool_fetch_usajobs(inp["keyword"], cfg, seen)
                n_new = sum(
                    1 for l in result.get("listings", []) if not l.get("already_seen")
                )
                print(
                    f"     → {len(result.get('listings', []))} listing(s) ({n_new} new)"
                )

            elif name == "send_report":
                result = tool_send_report(
                    inp["jobs"], inp["summary"], cfg, run_date, dry_run
                )
                final_jobs = inp["jobs"]
                status = result.get("status")
                print(f"     → {result['job_count']} job(s) — status: {status}")
                report_sent = True

            else:
                result = {"error": f"Unknown tool: {name}"}

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                }
            )

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if report_sent:
            break

    else:
        print(f"\n⚠️  Safety limit reached ({MAX_ITERS} iterations).")

    return final_jobs, report_sent


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Job Hunter Agent")
    parser.add_argument(
        "-n",
        "--results",
        type=int,
        default=5,
        metavar="N",
        help="Stop after finding N HIGH-priority fresh listings (default: 5)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear the seen-jobs log before running (re-surfaces all listings)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Search and score but print results instead of sending email",
    )
    args = parser.parse_args()

    cfg = load_config()
    run_date = datetime.now().strftime("%Y-%m-%d %I:%M %p")

    api_key = cfg.get("anthropic_api_key", "ENV")
    if api_key != "ENV":
        os.environ["ANTHROPIC_API_KEY"] = api_key

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "❌ ANTHROPIC_API_KEY not set — add it to keys.yaml or your environment."
        )

    seen = {} if args.reset else load_seen()
    if args.reset:
        print("🗑️  Seen-jobs log cleared (--reset)")
    else:
        print(f"📋 Seen-jobs log: {len(seen)} previously seen listings will be skipped")

    final_jobs, sent = run_agent(cfg, args.results, seen, run_date, args.dry_run)

    if sent and not args.dry_run and final_jobs:
        save_seen(seen, final_jobs, run_date)

    print("\n🏁 Done.\n")


if __name__ == "__main__":
    main()
