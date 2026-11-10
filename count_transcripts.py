import csv, re, time, os
from datetime import date
from urllib.parse import quote
import requests
from bs4 import BeautifulSoup

LIMIT = int(os.environ.get("LIMIT") or 0)
MON = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}
today = date.today()
cutoff = (today.year - 2) * 12 + today.month

s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0"

def fetch(code):
    r = None
    for path in (f"{quote(code)}/consolidated/", f"{quote(code)}/"):
        for _ in range(3):
            r = s.get(f"https://www.screener.in/company/{path}", timeout=30)
            if r.status_code == 429:
                time.sleep(60)
                continue
            break
        if r.ok:
            return r
    return r

rows = list(csv.DictReader(open("universe.csv", encoding="utf-8-sig")))
if LIMIT:
    rows = rows[:LIMIT]

out = []
for i, row in enumerate(rows, 1):
    code = (row.get("NSE Code") or "").strip() or (row.get("BSE Code") or "").strip()
    total = recent = 0
    status = "no code"
    if code:
        r = fetch(code)
        status = r.status_code
        if r.ok:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a"):
                if a.get_text(strip=True).lower() != "transcript":
                    continue
                total += 1
                li = a.find_parent("li")
                m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{4})",
                              li.get_text(" ") if li else "")
                if m and int(m.group(2)) * 12 + MON[m.group(1)] > cutoff:
                    recent += 1
    out.append({"Name": row["Name"], "Code": code, "Industry": row.get("Industry", ""),
                "Status": status, "Transcripts_total": total, "Transcripts_2yr": recent})
    print(f"{i}/{len(rows)} {row['Name']}: status {status}, total {total}, last 2 yrs {recent}", flush=True)
    time.sleep(2)

with open("transcript_counts.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=out[0].keys())
    w.writeheader()
    w.writerows(out)

print("Companies checked:", len(out))
print("With a transcript in last 2 yrs:", sum(1 for o in out if o["Transcripts_2yr"] > 0))
print("Total transcripts in last 2 yrs:", sum(o["Transcripts_2yr"] for o in out))
