"""Step 1 of the backfill: list every concall transcript from the last 24 months.

Reads  data/universe.csv  (the private data repo, checked out into ./data)
Writes data/manifest.csv  one row per transcript, status = pending

ONLY_LIST=1: scan only the tracking list (data/value_chain_leaders.csv) and MERGE
into the existing manifest: a company's rows are replaced only if its page loaded,
so a failed fetch never wipes earlier rows. Used for the quarterly refresh.

Fixes from the counting run:
- finds the date by walking up from the Transcript link, not only from its <li>
- records transcripts whose link has no URL, instead of silently skipping them
- retries on rate limits
- also lists call recordings ("REC" links) as status = audio
"""
import csv, os, re, time
from datetime import date
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

UNIVERSE = "data/universe.csv"
MANIFEST = "data/manifest.csv"
LIST = "data/value_chain_leaders.csv"
LIMIT = int(os.environ.get("LIMIT") or 0)
ONLY_LIST = os.environ.get("ONLY_LIST") == "1"

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MON = {m: i for i, m in enumerate(MONTHS, 1)}
DATE_RE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{4})\b")
today = date.today()
CUTOFF = (today.year - 2) * 12 + today.month  # keep the last 24 months

s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0"


def get(url):
    for attempt in range(4):
        try:
            r = s.get(url, timeout=30)
        except requests.RequestException:
            time.sleep(10)
            continue
        if r.status_code == 429:
            time.sleep(60 * (attempt + 1))
            continue
        return r
    return None


def fetch_company(code):
    for path in (f"{quote(code)}/consolidated/", f"{quote(code)}/"):
        r = get(f"https://www.screener.in/company/{path}")
        if r is not None and r.ok and "transcript" in r.text.lower():
            return r
    return r


def find_date(el):
    """Walk up from the link until an ancestor's own text holds a month-year."""
    node = el
    for _ in range(5):
        node = node.parent
        if node is None:
            return None
        m = DATE_RE.search(node.get_text(" "))
        if m:
            return m
    return None


def link_of(el, base):
    for attr in ("href", "data-url", "data-href", "data-src"):
        v = el.get(attr)
        if v and not v.startswith("#") and not v.startswith("javascript"):
            return urljoin(base, v)
    return ""


if ONLY_LIST:
    rows = [{"NSE Code": r["Code"], "Name": r["Company"], "Industry": r["Sub-industry"]}
            for r in csv.DictReader(open(LIST, encoding="utf-8"))]
else:
    rows = list(csv.DictReader(open(UNIVERSE, encoding="utf-8-sig")))
if LIMIT:
    rows = rows[:LIMIT]

out, seen, scanned = [], set(), set()
for i, row in enumerate(rows, 1):
    code = (row.get("NSE Code") or "").strip() or (row.get("BSE Code") or "").strip()
    if not code:
        continue
    r = fetch_company(code)
    found = 0
    if r is not None and r.ok:
        soup = BeautifulSoup(r.text, "html.parser")
        # Any tag whose own text is "Transcript": linked (<a>) or not yet linked (<div>/<span>,
        # shown greyed by Screener when the call is held but the PDF is not attached).
        # Innermost match only, so <a><span>Transcript</span></a> is counted once.
        # "REC" = the call recording (audio/video): kept as status "audio" for transcribe_audio.py.
        for el in soup.find_all(True):
            label = el.get_text(strip=True).lower()
            if label not in ("transcript", "rec"):
                continue
            if any(ch.get_text(strip=True).lower() == label for ch in el.find_all(True)):
                continue
            m = find_date(el)
            if not m:
                continue
            ym = int(m.group(2)) * 12 + MON[m.group(1)]
            if ym <= CUTOFF:
                continue
            period = f"{m.group(2)}-{MON[m.group(1)]:02d}"
            url = link_of(el, r.url)
            if not url and el.find_parent("a") is not None:
                url = link_of(el.find_parent("a"), r.url)
            if label == "rec" and not url:
                continue
            key = (code, period, url)
            if key in seen:
                continue
            seen.add(key)
            if label == "rec":
                out.append({"code": code, "name": row["Name"], "industry": row.get("Industry", ""),
                            "period": period, "url": url, "status": "audio"})
                continue
            found += 1
            out.append({"code": code, "name": row["Name"],
                        "industry": row.get("Industry", ""),
                        "period": period, "url": url,
                        "status": "pending" if url else "no_link"})
    if r is not None and r.ok:
        scanned.add(code)
    status = r.status_code if r is not None else "error"
    print(f"{i}/{len(rows)} {row['Name']}: page {status}, transcripts {found}", flush=True)
    time.sleep(2)

if ONLY_LIST and os.path.exists(MANIFEST):
    kept = [m for m in csv.DictReader(open(MANIFEST, encoding="utf-8"))
            if m["code"] not in scanned]
    print(f"Merge: kept {len(kept)} rows of companies not rescanned, "
          f"replaced rows for {len(scanned)} companies")
    out = kept + out

with open(MANIFEST, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["code", "name", "industry", "period", "url", "status"])
    w.writeheader()
    w.writerows(out)

print("Transcripts listed:", len(out))
print("Without a link:", sum(1 for o in out if o["status"] == "no_link"))
print("Recordings (REC):", sum(1 for o in out if o["status"] == "audio"))
print("Companies with transcripts:", len({o["code"] for o in out}))
