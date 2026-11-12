"""Fetch Q1 FY27 concall transcripts for the tracking list into a reading queue.

Reads  data/value_chain_leaders.csv  (companies to read: Read wave = Wave 1 / Wave 2)
       data/manifest.csv             (every transcript link from list_transcripts.py)
Writes data/queue/Q1FY27/<CODE>.txt  one text file per company (latest call Jul-Sep 2026)
       data/queue/Q1FY27/_log.csv    status per company

Resumable: companies whose text file already exists are skipped.
"""
import csv, os, re, subprocess, tempfile, time

import requests

LIST = "data/value_chain_leaders.csv"
MANIFEST = "data/manifest.csv"
OUT = "data/queue/Q1FY27"
WINDOW = ("2026-07", "2026-09")      # Q1 FY27 calls
MIN_CHARS = 3000                     # less = scanned image PDF or broken file
LIMIT = int(os.environ.get("LIMIT") or 0)

os.makedirs(OUT, exist_ok=True)
s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0"


def safe(code):
    return re.sub(r"[^A-Za-z0-9_-]", "_", code)


def pdf_text(url):
    for _ in range(3):
        try:
            r = s.get(url, timeout=60)
        except requests.RequestException:
            time.sleep(5)
            continue
        if r.ok and r.content[:4] == b"%PDF":
            with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
                f.write(r.content)
                f.flush()
                out = subprocess.run(["pdftotext", f.name, "-"], capture_output=True, text=True)
            text = re.sub(r"[ \t]+", " ", out.stdout)
            return re.sub(r"\n{3,}", "\n\n", text)
        time.sleep(5)
    return None


# latest Q1 call per company
calls = {}
for row in csv.DictReader(open(MANIFEST, encoding="utf-8")):
    if WINDOW[0] <= row["period"] <= WINDOW[1] and row["url"]:
        if row["code"] not in calls or row["period"] > calls[row["code"]]["period"]:
            calls[row["code"]] = row

companies = [r for r in csv.DictReader(open(LIST, encoding="utf-8"))
             if r["Read wave"] in ("Wave 1", "Wave 2")]
companies.sort(key=lambda r: r["Read wave"])          # Wave 1 first
if LIMIT:
    companies = companies[:LIMIT]

log = []
for n, c in enumerate(companies, 1):
    code = c["Code"]
    path = os.path.join(OUT, safe(code) + ".txt")
    call = calls.get(code)
    if call is None:
        status = "no_q1_call"
    elif os.path.exists(path):
        status = "already_done"
    else:
        text = pdf_text(call["url"])
        if text is None:
            status = "download_failed"
        elif len(text) < MIN_CHARS:
            status = "no_text"
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"Company: {c['Company']}\nCode: {code}\n"
                        f"Value chain: {c['Value chain']}\nStage: {c['Stage']}\n"
                        f"Sub-industry: {c['Sub-industry']}\nRead wave: {c['Read wave']}\n"
                        f"Call month: {call['period']}\nSource: {call['url']}\n\n")
                f.write(text)
            status = "saved"
        time.sleep(2)
    log.append({"wave": c["Read wave"], "code": code, "company": c["Company"],
                "call_month": call["period"] if call else "", "status": status})
    print(f"{n}/{len(companies)} {c['Company']}: {status}", flush=True)

with open(os.path.join(OUT, "_log.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["wave", "code", "company", "call_month", "status"])
    w.writeheader()
    w.writerows(log)

counts = {}
for l in log:
    counts[l["status"]] = counts.get(l["status"], 0) + 1
print("Status counts:", counts)
