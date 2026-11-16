"""Fetch one quarter's concall transcripts for the tracking list into a reading queue.

QUARTER=Q2FY27 (default). The Q1FY27 queue is final and is not touched.
Add a CONFIG entry to run a later quarter.

Reads  data/value_chain_leaders.csv  (companies to read)
       data/manifest.csv             (every transcript link, from list_transcripts.py)
       data/records/Q1FY27/          (Q1 records: baseline put on top of each Q2 file)
       data/q2_checklist.csv         (report Section 7 promises to test in Q2)
       data/extra_sources.csv        (hand-found transcript links, e.g. company websites)
Writes data/queue/<QUARTER>/<CODE>.txt  one text file per company
       data/queue/<QUARTER>/_log.csv    status per company
       data/queue/<QUARTER>/_missing.csv  companies still without a transcript
       data/queue/<QUARTER>/_rejected/    calls that failed the quarter check, kept for review

Sources: Screener (manifest) + hand-found links, then joint call. Every source passes the
same quarter check. (BSE filings were tried and dropped: BSE refuses GitHub's servers.)

Resumable: saved files are skipped, so it can be rerun as late transcripts are filed.
Files with a non-web Source (manual uploads) are never replaced; only the
Q1 baseline is added on top of them, the transcript text is left as it is.
"""
import csv, glob, os, re, subprocess, tempfile, time

import requests

LIST = "data/value_chain_leaders.csv"
MANIFEST = "data/manifest.csv"
QUARTER = os.environ.get("QUARTER") or "Q2FY27"
LIMIT = int(os.environ.get("LIMIT") or 0)
MIN_CHARS = 3000                     # less = scanned image PDF or broken file

# A real Q2 call names Q2/H1 FY27 more often than Q1 FY27. Q1 calls also say "Q2 FY27"
# (guidance), so a single mention is not enough: tested on the 468 Q1 transcripts,
# "any mention" wrongly passed 83 of them, "Q2 mentions > Q1 mentions" passed 1.
Q2_RE = re.compile(r"Q2\s*-?\s*(FY|F\.Y\.)\s*'?(20)?27|H1\s*-?\s*(FY|F\.Y\.)\s*'?(20)?27|"
                   r"(second|2nd) quarter (and (the )?(first )?half[- ]year )?(of |for )?(the )?"
                   r"(fiscal|financial year|FY)\s*'?(20)?(27|2026-27)|"
                   r"(quarter|half[- ]?year|six months) (and (half[- ]year|six months) )?ended (on )?"
                   r"(30(th)? sep(tember|t)?\.?,? 2026|sep(tember|t)?\.? 30(th)?,? 2026)|"
                   r"[QH]2 (of )?(FY ?)?2026-27|Q2\s*(FY)?\s*2027|Q2'27", re.I)
Q1_RE = re.compile(r"Q1\s*-?\s*(FY|F\.Y\.)\s*'?(20)?27|(first|1st) quarter (of |for )?(the )?"
                   r"(fiscal|financial year|FY)\s*'?(20)?(27|2026-27)|"
                   r"quarter ended (on )?(30(th)? june,? 2026|june 30(th)?,? 2026)|"
                   r"Q1 (of )?(FY ?)?2026-27|Q1\s*(FY)?\s*2027|Q1'27", re.I)

# Calendar-year reporters (Castrol, Varun Beverages...): the Sep quarter is their Q3 / nine months.
CY_RE = re.compile(r"Q3\s*(CY|C\.Y\.)?\s*'?(20)?26\b|Q3 (of )?(CY|calendar year )?(20)?26|"
                   r"third quarter (of )?(20|CY ?|calendar year ?)?26|"
                   r"(nine|9) months ended (on )?(30(th)? sep|sep(tember|t)?\.? 30)", re.I)
# Weak fallback when a call never writes "FY27": bare Q2/H1 words must outnumber bare Q1 words.
# Tested on the 468 Q1 transcripts it wrongly passes 14 (3%), so files saved this way are flagged.
WEAK_THIS = re.compile(r"\b(Q2|H1|second quarter|first half|september quarter|sept?(ember)?[- ]quarter)\b", re.I)
WEAK_PREV = re.compile(r"\b(Q1|first quarter|june quarter)\b", re.I)

CONFIGS = {
    # Every Oct-Dec call must pass the Q2 check (Oct-Nov also hold investor meets).
    # "Not read" = the 11 names with no Q1 call: try again for a first baseline.
    "Q2FY27": dict(window=("2026-10", "2026-12"), this=(Q2_RE, CY_RE), prev=Q1_RE,
                   weak_this=WEAK_THIS, weak_prev=WEAK_PREV,
                   waves=("Wave 1", "Wave 2", "Not read")),
}
if QUARTER not in CONFIGS:
    raise SystemExit(f"No config for {QUARTER}; the Q1FY27 queue is final.")
CONFIG = CONFIGS[QUARTER]

ANNUAL_ONLY = {"WEALTH"}                 # Wealth First: annual (Q4) call only
JOINT_CALL = {"BHARTIHEXA": "BHARTIARTL"}  # Hexacom notes come from Airtel's call
BASELINE_MARK = "=== Q1 FY27 BASELINE"
TRANSCRIPT_MARK = f"=== {QUARTER[:2]} FY27 TRANSCRIPT ==="
BASELINE_SECTIONS = ("Verdict", "Promises to track", "Scores")

GOOD = {"saved", "saved_weak_match", "saved_joint", "already_done", "manual_kept", "annual_only"}

OUT = f"data/queue/{QUARTER}"
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


def score(text):
    """(strength, score): 2 = names the quarter (FY27 or calendar-year wording) more than Q1 FY27;
    1 = never names it but bare Q2/H1 words outnumber bare Q1 words (weak: flagged); 0 = reject."""
    head = text[:20000]
    hits = sum(len(r.findall(head)) for r in CONFIG["this"])
    if hits:
        sc = hits - len(CONFIG["prev"].findall(head))
        return (2, sc) if sc > 0 else (0, sc)
    a, b = len(CONFIG["weak_this"].findall(text)), len(CONFIG["weak_prev"].findall(text))
    return (1, a - b) if a >= 3 and a > b else (0, a - b)


def source_of(path):
    head = open(path, encoding="utf-8").read(3000)
    m = re.search(r"^Source: (.*)$", head, re.M)
    return m.group(1).strip() if m else ""


# ---------- Q1 baseline (records + Section 7 checklist) ----------
records = {os.path.basename(p)[:-3]: p for p in glob.glob("data/records/Q1FY27/*/*/*.md")}
checklist = {}
if os.path.exists("data/q2_checklist.csv"):
    for r in csv.DictReader(open("data/q2_checklist.csv", encoding="utf-8")):
        checklist.setdefault(r["code"], []).append(r)


def baseline(code):
    lines = [f"{BASELINE_MARK} (from records/Q1FY27; mark each promise kept / partly / missed) ==="]
    path = records.get(code)
    if path:
        text = open(path, encoding="utf-8").read()
        for name in BASELINE_SECTIONS:
            m = re.search(rf"^## {re.escape(name)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
            if m:
                lines += [f"## {name}", m.group(1).strip(), ""]
    else:
        lines += ["No Q1 record (company was 'Not read' in Q1): this Q2 record is its first baseline.", ""]
    for r in checklist.get(code, []):
        lines += ["## Master report checklist (Section 7)",
                  f"- Q1 promise or flag: {r['q1_promise_or_flag']}",
                  f"- Q2 test: {r['q2_test']}", ""]
    return "\n".join(lines).rstrip() + "\n\n"


def header(c, call_month, source, note=""):
    h = (f"Company: {c['Company']}\nCode: {c['Code']}\n"
         f"Value chain: {c['Value chain']}\nStage: {c['Stage']}\n"
         f"Sub-industry: {c['Sub-industry']}\nRead wave: {c['Read wave']}\n"
         f"Quarter: {QUARTER}\nCall month: {call_month}\nSource: {source}\n")
    return h + (f"Note: {note}\n" if note else "") + "\n"


def add_baseline_to_manual(path, code):
    text = open(path, encoding="utf-8").read()
    if BASELINE_MARK in text:
        return
    head, _, body = text.partition("\n\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n\n" + baseline(code) + TRANSCRIPT_MARK + "\n\n" + body)


# ---------- candidate calls per company ----------
used_q1 = {source_of(p) for p in glob.glob("data/queue/Q1FY27/*.txt")}

cands = {}
for row in csv.DictReader(open(MANIFEST, encoding="utf-8")):
    w = CONFIG["window"]
    if w[0] <= row["period"] <= w[1] and row["url"] and row["url"] not in used_q1:
        cands.setdefault(row["code"], []).append(row)
# Screener shows "Transcript" without a link: the call happened, transcript not attached yet
pending = {r["code"] for r in csv.DictReader(open(MANIFEST, encoding="utf-8"))
           if CONFIG["window"][0] <= r["period"] <= CONFIG["window"][1] and not r["url"]}
if os.path.exists("data/extra_sources.csv"):              # hand-found links
    for r in csv.DictReader(open("data/extra_sources.csv", encoding="utf-8")):
        if r.get("quarter", QUARTER) == QUARTER and r.get("url", "").startswith("http"):
            cands.setdefault(r["code"].strip(), []).append(
                {"code": r["code"].strip(), "period": "manual-link", "url": r["url"].strip()})

REJECTED = os.path.join(OUT, "_rejected")


def pick(code):
    """Download every candidate call in the window; keep the one that is most clearly
    this quarter's results call (strong match first, then score; ties: earliest).
    Rejected texts are kept in _rejected/ so they can be checked by hand."""
    rows = sorted(cands.get(code, []), key=lambda r: r["period"])
    if not rows:
        return None, None, ("call_done_transcript_pending" if code in pending else "no_call_yet"), ""
    best, status, rejected = None, "not_quarter_call", []
    for row in rows:
        text = pdf_text(row["url"])
        time.sleep(2)
        if text is None:
            status = "download_failed" if best is None else status
            continue
        if len(text) < MIN_CHARS:
            status = "no_text" if best is None else status
            continue
        strength, sc = score(text)
        if strength and (best is None or (strength, sc) > best[:2]):
            best = (strength, sc, row, text)
        elif not strength:
            rejected.append((row, text))
    if best is None:                       # keep the texts and links for a hand check
        os.makedirs(REJECTED, exist_ok=True)
        for i, (row, text) in enumerate(rejected, 1):
            with open(os.path.join(REJECTED, f"{safe(code)}_{i}.txt"), "w", encoding="utf-8") as f:
                f.write(f"Code: {code}\nCall month: {row['period']}\nSource: {row['url']}\n\n{text}")
        return None, None, status, " ".join(r["url"] for r in rows)
    return best[2], best[3], ("saved" if best[0] == 2 else "saved_weak_match"), ""


companies = [r for r in csv.DictReader(open(LIST, encoding="utf-8"))
             if r["Read wave"] in CONFIG["waves"]]
companies.sort(key=lambda r: r["Read wave"])          # Not read, Wave 1, Wave 2
if LIMIT:
    companies = companies[:LIMIT]

log, picked = [], {}
for n, c in enumerate(companies, 1):
    code = c["Code"]
    path = os.path.join(OUT, safe(code) + ".txt")
    call_month, source, evidence = "", "", ""
    if os.path.exists(path) and not source_of(path).startswith("http"):
        status = "manual_kept"                        # user-supplied: never replaced
        source = source_of(path) or "manual"
        add_baseline_to_manual(path, code)
    elif os.path.exists(path):
        status, source = "already_done", source_of(path)
    elif code in ANNUAL_ONLY:
        status = "annual_only"
    else:
        row, text, status, source = pick(code)
        note = ""
        if row is None and code in JOINT_CALL:
            partner = JOINT_CALL[code]
            row, text = picked.get(partner, (None, None))
            if row is None:
                row, text, _, _ = pick(partner)
            if row is not None:
                status, note = "saved_joint", f"Joint call with {partner}"
        if status == "saved_weak_match":
            note = "Weak quarter match: confirm the call date before reading"
        if row is None:
            evidence, source = (f"Screener links: {source}" if source else ""), ""
        if row is not None:
            picked[code] = (row, text)
            call_month, source = row["period"], row["url"]
            with open(path, "w", encoding="utf-8") as f:
                f.write(header(c, call_month, source, note))
                f.write(baseline(code) + TRANSCRIPT_MARK + "\n\n")
                f.write(text)
    log.append({"wave": c["Read wave"], "code": code, "company": c["Company"],
                "chain": c["Value chain"], "sub_industry": c["Sub-industry"],
                "call_month": call_month, "status": status, "source": source,
                "evidence": evidence})
    print(f"{n}/{len(companies)} {c['Company']}: {status}", flush=True)

FIELDS = ["wave", "code", "company", "chain", "sub_industry", "call_month", "status", "source", "evidence"]
with open(os.path.join(OUT, "_log.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=FIELDS)
    w.writeheader()
    w.writerows(log)
# What needs a hand-found link: call clearly held first, then announced, then the rest
order = {"call_done_transcript_pending": 0, "download_failed": 1, "no_text": 2, "not_quarter_call": 3}
missing = sorted([l for l in log if l["status"] not in GOOD],
                 key=lambda l: (order.get(l["status"], 2), l["chain"], l["wave"]))
with open(os.path.join(OUT, "_missing.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=FIELDS)
    w.writeheader()
    w.writerows(missing)
print(f"Missing a transcript: {len(missing)} (see _missing.csv)")

counts = {}
for l in log:
    counts[l["status"]] = counts.get(l["status"], 0) + 1
print("Status counts:", counts)
