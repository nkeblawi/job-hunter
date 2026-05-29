#!/usr/bin/env python3
"""
Job Hunter Agent — Nabeel Keblawi
Single file. All source logic is in clearly labelled sections.
Prompts and candidate profile live in config.yaml so you can tweak without touching code.

Usage:
  python job_hunter.py              # returns top 10 (default)
  python job_hunter.py -n 5        # returns top 5
  python job_hunter.py -n 20       # returns top 20
"""

import argparse
import json
import os
import re
import smtplib
import time
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
import requests
import yaml
from bs4 import BeautifulSoup


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

def load_config(path="config.yaml", keys_path="keys.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    # Merge credentials from keys.yaml (values in keys.yaml take precedence)
    try:
        with open(keys_path) as f:
            keys = yaml.safe_load(f)
        if keys:
            cfg.update(keys)
    except FileNotFoundError:
        print(f"⚠️  {keys_path} not found — using config.yaml values or ENV vars only.")
    return cfg


# ══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def is_recent(date_str: str, days: int = 3) -> bool:
    """True if date_str is within the last N days. Handles ISO 8601."""
    if not date_str or str(date_str).lower() == "unknown":
        return False
    try:
        dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        return dt >= datetime.now(timezone.utc) - timedelta(days=days)
    except Exception:
        return False


def clean_json(raw: str) -> str:
    """Extract the first valid JSON array from Claude's response, handling fences and prose."""
    raw = raw.strip()
    # Strip markdown fences
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()
    # Find the first [ and extract exactly to its matching ]
    start = raw.find("[")
    if start == -1:
        return "[]"
    depth = 0
    for i, ch in enumerate(raw[start:], start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return raw[start:i+1]
    # Unclosed array — return from [ onward and let caller handle it
    return raw[start:]


# ══════════════════════════════════════════════════════════════════════════════
# CLAUDE SCORING  (shared by all sources)
# ══════════════════════════════════════════════════════════════════════════════

def score_listings(listings: list[dict], prompts: dict) -> list[dict]:
    """
    Score a list of pre-fetched listings with Claude.
    Uses prompts['score_system'] + prompts['candidate_profile'] from config.
    Processes in batches of 5. Retries up to 3x on overloaded errors.
    Never raises — returns unscored on failure.
    """
    if not listings:
        return []

    system_prompt = (
        prompts.get("score_system", "Score these job listings against the candidate profile.")
        + "\n\nCANDIDATE PROFILE:\n"
        + prompts.get("candidate_profile", "")
    )

    sources = [l.get("source", "unknown") for l in listings]
    clean   = [{k: v for k, v in l.items() if k != "source"} for l in listings]
    client  = anthropic.Anthropic()
    scored_all = []

    for i in range(0, len(clean), 5):
        batch         = clean[i:i + 5]
        batch_sources = sources[i:i + 5]
        batch_num     = i // 5 + 1
        success = False
        for attempt in range(3):
            try:
                response = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=3000,
                    system=system_prompt,
                    messages=[{
                        "role": "user",
                        "content": f"Score these {len(batch)} listings:\n\n{json.dumps(batch, indent=2)}"
                    }]
                )
                scored_batch = json.loads(clean_json(response.content[0].text))
                for j, item in enumerate(scored_batch):
                    item["source"] = batch_sources[j] if j < len(batch_sources) else "unknown"
                scored_all.extend(scored_batch)
                success = True
                break
            except Exception as e:
                is_overloaded = "529" in str(e) or "overloaded" in str(e).lower()
                if is_overloaded and attempt < 2:
                    wait = 15 * (attempt + 1)
                    print(f"    ⏳ API overloaded (batch {batch_num}), retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"    ⚠️  Scoring error (batch {batch_num}): {e}")
                    for j, item in enumerate(batch):
                        item["source"] = batch_sources[j] if j < len(batch_sources) else "unknown"
                        item.setdefault("fit_score", 0)
                        item.setdefault("priority",  "LOW")
                        item.setdefault("rationale", "Scoring failed — review manually.")
                        item.setdefault("skills_gap", "—")
                        item.setdefault("flags",     "scoring error")
                    scored_all.extend(batch)
                    break

    return scored_all


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE: GREENHOUSE API
# Public GET API — no auth needed.
# Token check: https://boards.greenhouse.io/TOKEN  (200 = valid, 404 = wrong)
# ══════════════════════════════════════════════════════════════════════════════

def run_greenhouse(orgs: list[dict], prompts: dict, days: int = 3) -> tuple[list[dict], list[str]]:
    BASE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    all_listings, warnings = [], []

    for org in orgs:
        name  = org["name"]
        token = org["board_token"]
        print(f"  → {name} (Greenhouse) ...", end="", flush=True)
        try:
            resp = requests.get(BASE.format(token=token), params={"content": "true"}, timeout=15)
            if resp.status_code == 404:
                raise ValueError(f"Board token '{token}' not found — verify at boards.greenhouse.io/{token}")
            resp.raise_for_status()

            TITLE_KEYWORDS = [
                "data", "analyst", "engineer", "scientist", "analytics",
                "machine learning", "ml", "ai", "intelligence", "python",
                "cloud", "etl", "pipeline", "meteorolog", "climate",
                "weather", "atmospheric", "research", "snowflake", "sql",
            ]
            OVERFLOW_LIMIT = 50

            all_jobs = resp.json().get("jobs", [])

            # Step 1: Title filter first — cheapest cut
            title_matched = []
            for job in all_jobs:
                title = job.get("title", "").lower()
                if any(kw in title for kw in TITLE_KEYWORDS):
                    title_matched.append(job)

            # Step 2: Date filter using created_at (more reliable) with updated_at fallback
            def job_date(j):
                return j.get("created_at") or j.get("updated_at") or ""

            date_filtered = [j for j in title_matched if not job_date(j) or is_recent(job_date(j), days)]

            # Step 3: If date filter isn't working (all pass), cap at OVERFLOW_LIMIT most recent
            careers_url = f"https://boards.greenhouse.io/{token}"
            if len(date_filtered) > OVERFLOW_LIMIT:
                # Sort by date descending and take top OVERFLOW_LIMIT
                date_filtered.sort(key=lambda j: job_date(j), reverse=True)
                overflow_count = len(date_filtered)
                date_filtered = date_filtered[:OVERFLOW_LIMIT]
                print(f" ⚠️  {overflow_count} title-matched listings; capped at {OVERFLOW_LIMIT} most recent")
                warnings.append(
                    f"<b>{name}</b>: {overflow_count} relevant listings found but date filter unreliable — "
                    f"showing {OVERFLOW_LIMIT} most recent. "
                    f"&nbsp;<a href='{careers_url}' style='color:#1a73e8;font-size:11px'>[browse all jobs manually]</a>"
                )

            listings = []
            for job in date_filtered:
                listings.append({
                    "source":   "greenhouse_api",
                    "org":      name,
                    "title":    job.get("title", ""),
                    "location": job.get("location", {}).get("name", "Unknown"),
                    "salary":   "Not listed",
                    "posted":   job_date(job),
                    "url":      job.get("absolute_url", ""),
                })

            skipped_title = len(all_jobs) - len(title_matched)
            print(f" {len(listings)} relevant listing(s) ({skipped_title} skipped by title filter)")
            if listings:
                all_listings.extend(score_listings(listings, prompts))

        except Exception as e:
            print(f" ⚠️  ERROR")
            warnings.append(f"<b>{name}</b> (Greenhouse): {e}")

        time.sleep(0.5)

    return all_listings, warnings


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE: HTML FALLBACK
# For orgs without a public API. Works well on static pages (govt sites).
# Workday/iCIMS/Taleo are JS-rendered and will return little text — flagged.
# ══════════════════════════════════════════════════════════════════════════════

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

def run_html(orgs: list[dict], prompts: dict, days: int = 3) -> tuple[list[dict], list[str], list[dict]]:
    all_listings, warnings, also_ran_candidates = [], [], []

    # HTML pages use a combined extract+score prompt from config
    system_prompt = (
        prompts.get("html_extract_system", "Extract and score job listings from this careers page.")
        + "\n\nCANDIDATE PROFILE:\n"
        + prompts.get("candidate_profile", "")
    )
    client = anthropic.Anthropic()

    for org in orgs:
        name       = org["name"]
        if org.get("skip"):
            print(f"  → {name} [skipped — JS-rendered]")
            continue
        verify_ssl = not org.get("skip_ssl_verify", False)

        # Support both careers_url (single) and careers_urls (list) in config
        raw_urls = org.get("careers_urls") or [org.get("careers_url", "")]
        urls = [u for u in raw_urls if u]   # drop any empty strings

        # Accumulate page text across all URLs for this org, then score in one Claude call
        combined_text = ""
        first_url     = urls[0] if urls else ""
        url_labels    = []   # track which URL each chunk came from (for fallback links)

        import urllib3
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        for url in urls:
            label = url.split("keyword=")[-1].replace("+", " ") if "keyword=" in url else url
            print(f"  → {name} [{label}] ...", end="", flush=True)
            try:
                resp = requests.get(url, headers=BROWSER_HEADERS, timeout=15, verify=verify_ssl)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                page_text = soup.get_text(separator="\n", strip=True)

                if len(page_text) < 500:
                    warnings.append(
                        f"<b>{name}</b> [{label}]: JS-rendered — little text returned. "
                        f"&nbsp;<a href='{url}' style='color:#1a73e8;font-size:11px'>[open careers page]</a>"
                    )
                    print(f" ⚠️  JS-rendered")
                    continue

                chunk = page_text[:5000]   # per-URL cap; combined cap applied below
                combined_text += f"\n\n--- Search: {label} | URL: {url} ---\n{chunk}"
                url_labels.append((label, url))
                print(f" ✓ ({len(chunk)} chars)")

            except Exception as e:
                print(f" ⚠️  ERROR")
                warnings.append(
                    f"<b>{name}</b> [{label}]: {e} "
                    f"&nbsp;<a href='{url}' style='color:#1a73e8;font-size:11px'>[open careers page]</a>"
                )
            time.sleep(1)

        if not combined_text.strip():
            continue   # nothing fetched for this org

        # Pre-flight quality check: skip Claude call if text looks like pure nav/boilerplate
        JOB_SIGNALS = [
            "apply", "job", "position", "role", "engineer", "analyst", "scientist",
            "manager", "director", "remote", "salary", "full-time", "part-time",
            "experience", "required", "qualifications", "responsibilities",
            "opening", "hiring", "requisition", "posted", "vacancy",
        ]
        combined_lower = combined_text.lower()
        signal_hits = sum(1 for w in JOB_SIGNALS if w in combined_lower)
        if signal_hits < 2:
            print(f"  ⏭  {name}: page looks like nav/boilerplate ({signal_hits} job signals) — skipping Claude call")
            also_ran_candidates.append({"name": name, "url": first_url})
            continue

        # Single Claude call for all URLs combined (cap total at 12k chars)
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                system=system_prompt,
                messages=[{
                    "role": "user",
                    "content": f"ORG: {name}\n\nCAREERS PAGE TEXT:{combined_text[:12000]}"
                }]
            )
            raw = clean_json(response.content[0].text)
            listings = json.loads(raw)
            seen_titles = set()
            deduped     = []
            for l in listings:
                # Deduplicate: same title can appear across multiple keyword searches
                key = (l.get("title", "").lower().strip(), l.get("org", name).lower().strip())
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                l["source"] = "html"
                if not l.get("url"):
                    l["url"]        = first_url
                    l["url_direct"] = False
                else:
                    l["url_direct"] = True
                deduped.append(l)

            print(f"  ✅ {name}: {len(deduped)} listing(s) after dedup")
            all_listings.extend(deduped)

        except json.JSONDecodeError as e:
            preview = raw[:120].replace("<", "&lt;").replace(">", "&gt;") if "raw" in dir() else "(no response)"
            print(f"  ⚠️  {name}: non-JSON response — {preview!r}")
            warnings.append(
                f"<b>{name}</b>: Claude returned non-JSON (parse error: {e}). "
                f"Response started with: <code>{preview}</code> "
                f"&nbsp;<a href='{first_url}' style='color:#1a73e8;font-size:11px'>[open careers page]</a>"
            )
        except Exception as e:
            print(f"  ⚠️  {name}: {e}")
            warnings.append(
                f"<b>{name}</b>: {e} "
                f"&nbsp;<a href='{first_url}' style='color:#1a73e8;font-size:11px'>[open careers page]</a>"
            )

        time.sleep(1)

    return all_listings, warnings, also_ran_candidates


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE: USAJOBS API  (DISABLED — uncomment in main() when key is ready)
# To enable:
#   1. Email access@usajobs.gov to request a free API key
#   2. Add usajobs_api_key and usajobs_email to config.yaml
#   3. Uncomment the usajobs block in main() — nothing else changes
# ══════════════════════════════════════════════════════════════════════════════

USAJOBS_BASE    = "https://data.usajobs.gov/api/Search"
USAJOBS_SEARCHES = [
    {"Keyword": "data engineer",             "LocationName": "Virginia",     "RemoteIndicator": "True"},
    {"Keyword": "data scientist",            "LocationName": "Virginia",     "RemoteIndicator": "True"},
    {"Keyword": "data engineer",             "LocationName": "Washington DC"},
    {"Keyword": "data scientist",            "LocationName": "Washington DC"},
    {"Keyword": "meteorologist",             "LocationName": "Virginia"},
    {"Keyword": "machine learning engineer", "RemoteIndicator": "True"},
    {"Keyword": "business intelligence",     "LocationName": "Virginia"},
    {"Keyword": "analytics engineer",        "RemoteIndicator": "True"},
    {"Keyword": "research scientist data",   "LocationName": "Virginia"},
    # NOAA (agency code CM) — replaces HTML scraping of usajobs search pages
    {"Keyword": "physical scientist",        "Organization": "CM"},
    {"Keyword": "meteorologist",             "Organization": "CM"},
    {"Keyword": "information technology",    "Organization": "CM"},
    # USGS (agency code GS) — replaces HTML scraping of usajobs search pages
    {"Keyword": "physical scientist",        "Organization": "GS"},
    {"Keyword": "information technology",    "Organization": "GS"},
    {"Keyword": "operations research",       "Organization": "GS"},
]

def run_usajobs(cfg: dict, prompts: dict, days: int = 3) -> tuple[list[dict], list[str]]:
    api_key = cfg.get("usajobs_api_key", "")
    email   = cfg.get("usajobs_email", "")

    if not api_key or api_key == "YOUR_USAJOBS_API_KEY":
        return [], ["USAJobs: API key not configured — skipped."]

    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": email,
        "Authorization-Key": api_key,
    }
    seen_ids, raw_listings, warnings = set(), [], []

    print("  → USAJobs API ...", end="", flush=True)
    try:
        for params in USAJOBS_SEARCHES:
            query = {**params, "DatePosted": days, "ResultsPerPage": 25, "Fields": "min"}
            try:
                resp = requests.get(USAJOBS_BASE, headers=headers, params=query, timeout=15)
                resp.raise_for_status()
                for item in resp.json().get("SearchResult", {}).get("SearchResultItems", []):
                    d      = item.get("MatchedObjectDescriptor", {})
                    job_id = item.get("MatchedObjectId", "")
                    if job_id in seen_ids:
                        continue
                    seen_ids.add(job_id)
                    rem    = d.get("PositionRemuneration", [{}])
                    s_min  = rem[0].get("MinimumRange", "") if rem else ""
                    s_max  = rem[0].get("MaximumRange", "") if rem else ""
                    locs   = d.get("PositionLocation", [{}])
                    loc    = locs[0].get("LocationName", "Unknown") if locs else "Unknown"
                    remote = "remote" in loc.lower()
                    raw_listings.append({
                        "source":   "usajobs_api",
                        "org":      d.get("OrganizationName", d.get("DepartmentName", "Federal Agency")),
                        "title":    d.get("PositionTitle", ""),
                        "location": loc + (" [REMOTE]" if remote else ""),
                        "salary":   f"${s_min}–${s_max}" if s_min and s_max else "Not listed",
                        "posted":   d.get("PublicationStartDate", ""),
                        "url":      d.get("PositionURI", ""),
                    })
                time.sleep(0.5)
            except Exception as e:
                warnings.append(f"USAJobs search error ({params.get('Keyword')}): {e}")

        # Title filter — same keywords as Greenhouse, avoids scoring irrelevant roles
        TITLE_KEYWORDS_USA = [
            "data", "analyst", "engineer", "scientist", "analytics",
            "machine learning", "intelligence", "python", "cloud", "etl",
            "pipeline", "meteorolog", "climate", "weather", "atmospheric",
            "research", "snowflake", "sql", "information technology",
            "operations research", "physical scientist", "geospatial",
        ]
        filtered = [l for l in raw_listings if any(
            kw in l.get("title", "").lower() for kw in TITLE_KEYWORDS_USA
        )]
        skipped_title = len(raw_listings) - len(filtered)
        print(f" {len(raw_listings)} listing(s) found → {len(filtered)} after title filter ({skipped_title} skipped)")
        if not filtered:
            return [], warnings
        scored = score_listings(filtered, prompts)
        return scored, warnings

    except Exception as e:
        print(f" ⚠️  ERROR")
        return [], [f"USAJobs source crashed: {e}"]


# ══════════════════════════════════════════════════════════════════════════════
# SEEN-JOBS LOG  (seen_jobs.json)
# Tracks every job that has been emailed so it is never repeated in future runs.
# Key: "<org>|<title>" lowercased — simple and readable in the JSON file.
# ══════════════════════════════════════════════════════════════════════════════

SEEN_LOG = "seen_jobs.json"

def seen_key(job: dict) -> str:
    org   = job.get("org", "").lower().strip()
    title = job.get("title", "").lower().strip()
    return f"{org}|{title}"


def load_seen(path: str = SEEN_LOG) -> dict:
    """Return {key: {first_seen, title, org}} from the log file."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen: dict, emailed: list[dict], run_date: str, path: str = SEEN_LOG):
    """Add newly emailed jobs to the log and write it back to disk."""
    for job in emailed:
        k = seen_key(job)
        if k not in seen:
            seen[k] = {
                "first_seen": run_date,
                "title":      job.get("title", ""),
                "org":        job.get("org", ""),
            }
    with open(path, "w") as f:
        json.dump(seen, f, indent=2)
    print(f"  Seen-jobs log updated -> {len(seen)} total entries ({path})")


def filter_seen(listings: list[dict], seen: dict) -> tuple[list[dict], int]:
    """Remove listings already in the seen log. Returns (fresh_listings, skipped_count)."""
    fresh   = [l for l in listings if seen_key(l) not in seen]
    skipped = len(listings) - len(fresh)
    return fresh, skipped


# ══════════════════════════════════════════════════════════════════════════════
# RANKING
# ══════════════════════════════════════════════════════════════════════════════

def select_top_n(listings: list[dict], n: int = 5, max_per_org: int = 3) -> list[dict]:
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    # Exclude zero-score entries and placeholder "no listings found" results
    eligible = [
        l for l in listings
        if l.get("fit_score", 0) > 10
        and "no specific listings" not in l.get("title", "").lower()
        and "no listings" not in l.get("title", "").lower()
    ]
    eligible.sort(key=lambda x: (
        order.get(x.get("priority", "LOW"), 2),
        -x.get("fit_score", 0)
    ))
    # Cap at max_per_org per organization
    org_counts: dict[str, int] = {}
    result = []
    for job in eligible:
        org = job.get("org", "").lower().strip()
        if org_counts.get(org, 0) < max_per_org:
            result.append(job)
            org_counts[org] = org_counts.get(org, 0) + 1
        if len(result) >= n:
            break
    return result


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL
# ══════════════════════════════════════════════════════════════════════════════

SOURCE_BADGES = {
    "greenhouse_api": "🌿 Greenhouse",
    "html":           "🌐 Web",
    "usajobs_api":    "🏛️ USAJobs",
}

def build_email(top: list[dict], warnings: list[str], run_date: str,
               also_ran: list[dict] | None = None) -> str:
    """
    also_ran: list of {name, url} dicts — orgs that scraped cleanly but
              whose listings didn't make the top-N cut. Shown as browse links.
    """
    rows = ""
    for i, job in enumerate(top, 1):
        url        = job.get("url", "")
        url_direct = job.get("url_direct", True)   # False when url is a fallback careers page
        link_label = "Apply →" if url_direct else "Careers page →"
        link  = f'<a href="{url}" style="color:#2980b9;white-space:nowrap">{link_label}</a>' if url else "—"
        flags = (f'<br><span style="color:#c0392b;font-size:11px">⚑ {job["flags"]}</span>'
                 if job.get("flags") else "")
        score = job.get("fit_score", 0)
        sc    = "#27ae60" if score >= 75 else "#e67e22" if score >= 50 else "#c0392b"
        pri   = job.get("priority", "?")
        pc    = {"HIGH": "#27ae60", "MEDIUM": "#e67e22"}.get(pri, "#95a5a6")
        badge = SOURCE_BADGES.get(job.get("source", "html"), "🌐 Web")
        gap   = job.get("skills_gap", "") or ""
        gap_html = ""
        if gap and gap.strip() and gap.strip().lower() not in ("none", "—", "-", ""):
            gap_html = (
                f'<div style="margin-top:7px;padding:5px 8px;background:#fff8e1;'
                f'border-left:3px solid #f39c12;border-radius:3px;font-size:11px;color:#7d6608">'
                f'<strong>Gap:</strong> {gap}</div>'
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
          <td style="padding:11px;font-size:12px;color:#444">{job.get('rationale','')}{flags}{gap_html}</td>
          <td style="padding:11px">{link}</td>
        </tr>"""

    table = f"""
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
    </table>""" if top else """
    <div style="padding:30px;text-align:center;color:#888;background:#f9f9f9;border-radius:8px">
      No matching listings found today. Try again tomorrow or add more orgs to config.yaml.
    </div>"""

    warn_block = ""
    if warnings:
        items = "".join(f"<li style='margin:4px 0'>{w}</li>" for w in warnings)
        warn_block = f"""
        <div style="margin-top:24px;padding:14px;background:#fff8e1;
                    border-left:4px solid #f39c12;border-radius:4px">
          <strong>⚠️ Warnings (check these manually):</strong>
          <ul style="margin:8px 0 0 0">{items}</ul>
        </div>"""

    also_ran_block = ""
    if also_ran:
        links = ""
        for entry in sorted(also_ran, key=lambda x: x["name"]):
            links += (
                f'<a href="{entry["url"]}" style="display:inline-block;margin:4px 6px;' +
                f'padding:5px 12px;background:#eaf0fb;border:1px solid #c5d5ef;' +
                f'border-radius:14px;color:#2471a3;font-size:12px;text-decoration:none">' +
                f'{entry["name"]} →</a>'
            )
        also_ran_block = f"""
        <div style="margin-top:24px;padding:16px;background:white;border-radius:8px;
                    box-shadow:0 1px 3px rgba(0,0,0,.08)">
          <strong style="color:#2c3e50;font-size:13px">
            🔍 Also checked — no top matches, but scraped cleanly. Browse manually if interested:
          </strong>
          <div style="margin-top:10px;line-height:2">{links}</div>
        </div>"""

    return f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
                       max-width:1050px;margin:auto;padding:24px;background:#f5f7fa">
      <div style="background:white;border-radius:8px;padding:24px;
                  box-shadow:0 1px 3px rgba(0,0,0,.1)">
        <h2 style="color:#2c3e50;margin-top:0">🎯 Job Hunt Results — {run_date}</h2>
        <p style="color:#666;margin-bottom:20px">
          Top matches (≤3 days old where verifiable) ·
          Filters: remote/hybrid Loudoun Co. VA · $120K+ · no on-call · no sales/marketing
        </p>
        {table}
      </div>
      {also_ran_block}
      {warn_block}
      <p style="color:#bbb;font-size:11px;text-align:center;margin-top:16px">
        Job Hunter Agent · {run_date}
      </p>
    </body></html>"""


def send_email(html: str, cfg: dict, run_date: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎯 Job Hunt — {run_date}"
    msg["From"]    = cfg["email"]["from"]
    msg["To"]      = cfg["email"]["to"]
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL(cfg["email"]["smtp_host"], cfg["email"]["smtp_port"]) as s:
        s.login(cfg["email"]["username"], cfg["email"]["password"])
        s.sendmail(cfg["email"]["from"], cfg["email"]["to"], msg.as_string())
    print("✅ Email sent.")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Job Hunter Agent")
    parser.add_argument(
        "-n", "--results",
        type=int,
        default=10,
        metavar="N",
        help="Number of top listings to return (default: 10)"
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear the seen-jobs log before running (re-surfaces all previously seen listings)"
    )
    args = parser.parse_args()

    cfg      = load_config()
    prompts  = cfg.get("prompts", {})
    run_date = datetime.now().strftime("%Y-%m-%d %I:%M %p")

    # Set Anthropic API key
    api_key = cfg.get("anthropic_api_key", "ENV")
    if api_key != "ENV":
        os.environ["ANTHROPIC_API_KEY"] = api_key

    print(f"\n🔍 Job Hunter Agent — {run_date}\n")

    # ── Seen-jobs log ─────────────────────────────────────────────────────────
    if args.reset:
        seen = {}
        print("🗑️  Seen-jobs log cleared (--reset)\n")
    else:
        seen = load_seen()
        print(f"📋 Seen-jobs log: {len(seen)} previously emailed listings will be skipped\n")

    all_listings: list[dict] = []
    all_warnings: list[str]  = []

    # ── Greenhouse API ────────────────────────────────────────────────────────
    # Each source wrapped in try/except so one failure never kills the others.
    gh_orgs = cfg.get("greenhouse_orgs", [])
    if gh_orgs:
        print("[ Greenhouse API ]")
        try:
            listings, warnings = run_greenhouse(gh_orgs, prompts, days=3)
            all_listings.extend(listings)
            all_warnings.extend(warnings)
        except Exception as e:
            print(f"  💥 Greenhouse source crashed unexpectedly: {e}")
            all_warnings.append(f"Greenhouse source crashed: {e}")
        print()

    # ── HTML fallback ─────────────────────────────────────────────────────────
    html_orgs = cfg.get("html_orgs", [])
    all_html_also_ran: list[dict] = []
    if html_orgs:
        print("[ Web (HTML) ]")
        try:
            listings, warnings, html_also_ran = run_html(html_orgs, prompts, days=3)
            all_listings.extend(listings)
            all_warnings.extend(warnings)
            all_html_also_ran.extend(html_also_ran)
        except Exception as e:
            print(f"  💥 HTML source crashed unexpectedly: {e}")
            all_warnings.append(f"HTML source crashed: {e}")
        print()

    # ── USAJobs API ───────────────────────────────────────────────────────────
    print("[ USAJobs API ]")
    try:
        listings, warnings = run_usajobs(cfg, prompts, days=3)
        all_listings.extend(listings)
        all_warnings.extend(warnings)
    except Exception as e:
        print(f"  💥 USAJobs source crashed unexpectedly: {e}")
        all_warnings.append(f"USAJobs source crashed: {e}")
    print()

    # ── Filter seen, rank, send ───────────────────────────────────────────────
    fresh, skipped = filter_seen(all_listings, seen)
    print(f"📊 Total evaluated: {len(all_listings)}  |  Already seen: {skipped}  |  Fresh: {len(fresh)}")

    max_per_org = cfg.get("settings", {}).get("max_results_per_org", 3)
    top_n = select_top_n(fresh, n=args.results, max_per_org=max_per_org)
    print(f"   Top {len(top_n)} selected")

    if not top_n:
        print("   Nothing new to email — all listings already seen. Run with --reset to resurface.")
        return

    # ── Also-ran: orgs that scraped cleanly but didn't crack top N ──────────
    # Collect the org name of every listing that made it into top_n
    top_orgs = {j.get("org", "").lower().strip() for j in top_n}

    # Collect org names mentioned in any warning (they had errors — exclude)
    warned_orgs = set()
    for w in all_warnings:
        # Warnings are HTML like "<b>Org Name</b>: ..." — extract the bold text
        import re as _re
        for match in _re.findall(r"<b>(.*?)</b>", w):
            warned_orgs.add(match.lower().strip())

    # Build {org_name: first_url} from every listing that was evaluated
    org_urls: dict[str, str] = {}
    for job in all_listings:
        org = job.get("org", "").strip()
        if org and org not in org_urls:
            org_urls[org] = job.get("url", "")

    # Also include orgs from config that produced no listings at all but had no warning
    # (they may have had all listings filtered by seen-log; still worth a browse link)
    all_config_orgs: list[dict] = []
    for o in cfg.get("html_orgs", []):
        urls = o.get("careers_urls") or [o.get("careers_url", "")]
        all_config_orgs.append({"name": o["name"], "url": urls[0] if urls else ""})
    for o in cfg.get("greenhouse_orgs", []):
        all_config_orgs.append({
            "name": o.get("name", o.get("board_token", "")),
            "url":  f"https://boards.greenhouse.io/{o.get('board_token', '')}"
        })

    also_ran: list[dict] = []
    seen_also = set()
    # Seed with orgs flagged as boilerplate during HTML scraping
    for entry in all_html_also_ran:
        name_lc = entry["name"].lower().strip()
        if name_lc not in seen_also and entry.get("url"):
            also_ran.append(entry)
            seen_also.add(name_lc)
    for entry in all_config_orgs:
        name_lc = entry["name"].lower().strip()
        if (name_lc not in top_orgs
                and name_lc not in warned_orgs
                and name_lc not in seen_also
                and entry["url"]):
            also_ran.append(entry)
            seen_also.add(name_lc)

    print(f"   Also-ran orgs (clean, below cut): {len(also_ran)}")

    print("   Sending email ...")
    html = build_email(top_n, all_warnings, run_date, also_ran=also_ran)
    send_email(html, cfg, run_date)

    save_seen(seen, top_n, run_date)
    print("🏁 Done.\n")


if __name__ == "__main__":
    main()
