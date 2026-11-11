"""Step 1 of the backfill: list every concall transcript from the last 24 months.

Reads  data/universe.csv  (the private data repo, checked out into ./data)
Writes data/manifest.csv  one row per transcript, status = pending

Fixes from the counting run:
- finds the date by walking up from the Transcript link, not only from its <li>
- records transcripts whose link has no URL, instead of silently skipping them
- retries on rate limits
"""
import csv, os, re, time
from datetime import date
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

UNIVERSE = "data/universe.csv"
MANIFEST = "data/manifest.csv"
LIMIT = int(os.environ.get("LIMIT") or 0)

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


rows = list(csv.DictReader(open(UNIVERSE, encoding="utf-8-sig")))
if LIMIT:
    rows = rows[:LIMIT]

out, seen = [], set()
for i, row in enumerate(rows, 1):
    code = (row.get("NSE Code") or "").strip() or (row.get("BSE Code") or "").strip()
    if not code:
        continue
    r = fetch_company(code)
    found = 0
    if r is not None and r.ok:
        soup = BeautifulSoup(r.text, "html.parser")
        for el in soup.find_all(["a", "button"]):
            if el.get_text(strip=True).lower() != "transcript":
                continue
            m = find_date(el)
            if not m:
                continue
            ym = int(m.group(2)) * 12 + MON[m.group(1)]
            if ym <= CUTOFF:
                continue
            period = f"{m.group(2)}-{MON[m.group(1)]:02d}"
            url = link_of(el, r.url)
            key = (code, period, url)
            if key in seen:
                continue
            seen.add(key)
            found += 1
            out.append({"code": code, "name": row["Name"],
                        "industry": row.get("Industry", ""),
                        "period": period, "url": url,
                        "status": "pending" if url else "no_link"})
    status = r.status_code if r is not None else "error"
    print(f"{i}/{len(rows)} {row['Name']}: page {status}, transcripts {found}", flush=True)
    time.sleep(2)

with open(MANIFEST, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["code", "name", "industry", "period", "url", "status"])
    w.writeheader()
    w.writerows(out)

print("Transcripts listed:", len(out))
print("Without a link:", sum(1 for o in out if o["status"] == "no_link"))
print("Companies with transcripts:", len({o["code"] for o in out}))
