"""Fetch one quarter's concall transcripts for the tracking list into a reading queue.

QUARTER=Q2FY27 (default). The Q1FY27 queue is final and is not touched.
Add a CONFIG entry to run a later quarter.

Reads  data/value_chain_leaders.csv  (companies to read)
       data/manifest.csv             (every transcript link, from list_transcripts.py)
       data/records/Q1FY27/          (Q1 records: baseline put on top of each Q2 file)
       data/q2_checklist.csv         (report Section 7 promises to test in Q2)
       data/extra_sources.csv        (hand-found transcript links, e.g. company websites)
       data/universe.csv             (BSE codes for the BSE-filings backup)
Writes data/queue/<QUARTER>/<CODE>.txt  one text file per company
       data/queue/<QUARTER>/_log.csv    status per company
       data/queue/<QUARTER>/_missing.csv  companies still without a transcript, with BSE evidence

Sources, in order: Screener (manifest) + hand-found links -> joint call -> BSE filings.
Every source passes the same quarter check.

Resumable: saved files are skipped, so it can be rerun as late transcripts are filed.
Files with a non-web Source (manual uploads) are never replaced; only the
Q1 baseline is added on top of them, the transcript text is left as it is.
"""
import csv, glob, os, re, subprocess, tempfile, time
from datetime import date

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

CONFIGS = {
    # Every Oct-Dec call must pass the Q2 check (Oct-Nov also hold investor meets).
    # "Not read" = the 11 names with no Q1 call: try again for a first baseline.
    "Q2FY27": dict(window=("2026-10", "2026-12"), this=Q2_RE, prev=Q1_RE,
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

GOOD = {"saved", "saved_bse", "saved_joint", "already_done", "manual_kept", "annual_only"}
TRANSCRIPT_RE = re.compile(r"transcript", re.I)
RECORDING_RE = re.compile(r"audio|recording|video", re.I)
CALL_RE = re.compile(r"earnings call|conference call|con-?call|analyst|investors? (call|meet|conference)|"
                     r"post[- ]results|results call", re.I)

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
    """Mentions of this quarter minus mentions of the previous one (first 20k chars)."""
    head = text[:20000]
    hits = len(CONFIG["this"].findall(head))
    return hits - len(CONFIG["prev"].findall(head)) if hits else 0


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
if os.path.exists("data/extra_sources.csv"):              # hand-found links
    for r in csv.DictReader(open("data/extra_sources.csv", encoding="utf-8")):
        if r.get("quarter", QUARTER) == QUARTER and r.get("url", "").startswith("http"):
            cands.setdefault(r["code"].strip(), []).append(
                {"code": r["code"].strip(), "period": "manual-link", "url": r["url"].strip()})

# ---------- BSE filings backup ----------
bse_code = {}
for r in csv.DictReader(open("data/universe.csv", encoding="utf-8-sig")):
    b = (r.get("BSE Code") or "").strip()
    if b:
        bse_code[b] = b
        if (r.get("NSE Code") or "").strip():
            bse_code[r["NSE Code"].strip()] = b
bse = requests.Session()
bse.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                    "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com",
                    "Accept": "application/json, text/plain, */*"})
BSE_API = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno={page}"
           "&strCat=-1&strPrevDate={start}&strScrip={scrip}&strSearch=P&strToDate={end}"
           "&strType=C&subcategory=-1")
BSE_START = CONFIG["window"][0].replace("-", "") + "01"


def bse_announcements(scrip):
    """All BSE announcements for the scrip since the window start. None = BSE unreachable."""
    out = []
    for page in range(1, 9):
        url = BSE_API.format(page=page, start=BSE_START, scrip=scrip,
                             end=date.today().strftime("%Y%m%d"))
        data = None
        for attempt in range(3):
            try:
                r = bse.get(url, timeout=30)
                if r.ok:
                    data = r.json()
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(5 * (attempt + 1))
        if data is None:
            return None if page == 1 else out
        rows = data.get("Table") or []
        out += rows
        if len(rows) < 50:
            break
        time.sleep(1)
    return out


def bse_pdf(row):
    name = (row.get("ATTACHMENTNAME") or "").strip()
    return f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{name}" if name else ""


def from_bse(code):
    """Try BSE transcripts; otherwise say what BSE shows about the call."""
    scrip = bse_code.get(code) or (code if code.isdigit() else "")
    if not scrip:
        return None, None, "nse_only_check_by_hand", ""
    anns = bse_announcements(scrip)
    if anns is None:
        return None, None, "bse_unreachable", ""
    time.sleep(1)
    text_of = lambda a: f"{a.get('HEADLINE') or ''} {a.get('NEWSSUB') or ''} {a.get('SUBCATNAME') or ''}"
    best = None
    for a in [a for a in anns if TRANSCRIPT_RE.search(text_of(a))]:
        url = bse_pdf(a)
        if not url or url in used_q1:
            continue
        text = pdf_text(url) or pdf_text(url.replace("AttachLive", "AttachHis"))
        time.sleep(2)
        if text and len(text) >= MIN_CHARS:
            sc = score(text)
            if sc > 0 and (best is None or sc > best[0]):
                best = (sc, {"period": (a.get("NEWS_DT") or "")[:7], "url": url}, text)
    if best:
        return best[1], best[2], "saved_bse", ""
    rec = [a for a in anns if RECORDING_RE.search(text_of(a)) and CALL_RE.search(text_of(a))]
    call = [a for a in anns if CALL_RE.search(text_of(a))]
    if rec:
        a = rec[0]
        return None, None, "call_done_no_transcript", f"{(a.get('NEWS_DT') or '')[:10]} {a.get('HEADLINE') or ''}".strip()
    if call:
        a = call[0]
        return None, None, "call_announced_no_transcript", f"{(a.get('NEWS_DT') or '')[:10]} {a.get('HEADLINE') or ''}".strip()
    return None, None, "no_call_on_bse", ""


def pick(code):
    """Download every candidate call in the window; keep the one that is most clearly
    this quarter's results call (ties: earliest). Usually there is only one."""
    rows = sorted(cands.get(code, []), key=lambda r: r["period"])
    if not rows:
        return None, None, "no_call_yet", ""
    best, status = None, "not_quarter_call"
    for row in rows:
        text = pdf_text(row["url"])
        time.sleep(2)
        if text is None:
            status = "download_failed" if best is None else status
            continue
        if len(text) < MIN_CHARS:
            status = "no_text" if best is None else status
            continue
        sc = score(text)
        if sc > 0 and (best is None or sc > best[0]):
            best = (sc, row, text)
    if best is None:                       # log the links so they can be checked by hand
        return None, None, status, " ".join(r["url"] for r in rows)
    return best[1], best[2], "saved", ""


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
        if row is None and status in ("no_call_yet", "not_quarter_call", "download_failed", "no_text"):
            screener_status, screener_links = status, source
            brow, btext, bstatus, evidence = from_bse(code)
            if brow is not None:
                row, text, status, source = brow, btext, bstatus, ""
                note = "From BSE filings (not on Screener)"
            elif bstatus in ("bse_unreachable", "nse_only_check_by_hand"):
                status = f"{screener_status}+{bstatus}"
            else:
                status = bstatus
            if row is None:
                source = ""
                if screener_links:
                    evidence = (evidence + " | Screener links: " + screener_links).strip(" |")
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
order = {"call_done_no_transcript": 0, "call_announced_no_transcript": 1}
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
