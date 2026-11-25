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
same quarter check (qcheck.py). Audio/video links (Screener "REC", or audio links in
extra_sources.csv) are left to transcribe_audio.py, which only runs where no PDF was saved.
An audio-transcribed file is replaced by a PDF transcript that arrives later, unless that
company's record for the quarter is already written. (BSE filings were tried and dropped: BSE refuses GitHub's servers.)

Resumable: saved files are skipped, so it can be rerun as late transcripts are filed.
Files with a non-web Source (manual uploads) are never replaced; only the
Q1 baseline is added on top of them, the transcript text is left as it is.
"""
import csv, glob, os, re, subprocess, tempfile, time

import requests

from qcheck import CONFIGS, is_audio, score as qscore

LIST = "data/value_chain_leaders.csv"
MANIFEST = "data/manifest.csv"
QUARTER = os.environ.get("QUARTER") or "Q2FY27"
LIMIT = int(os.environ.get("LIMIT") or 0)
MIN_CHARS = 3000                     # less = scanned image PDF or broken file

if QUARTER not in CONFIGS:
    raise SystemExit(f"No config for {QUARTER}; the Q1FY27 queue is final.")
CONFIG = CONFIGS[QUARTER]

ANNUAL_ONLY = {"WEALTH"}                 # Wealth First: annual (Q4) call only
# Companies confirmed by Gaurav to have held no call this quarter (data/no_call.csv: code,quarter,note)
NO_CALL = {r["code"].strip() for r in csv.DictReader(open("data/no_call.csv", encoding="utf-8"))
           if r["quarter"].strip() == QUARTER} if os.path.exists("data/no_call.csv") else set()
JOINT_CALL = {"BHARTIHEXA": "BHARTIARTL"}  # Hexacom notes come from Airtel's call
BASELINE_MARK = "=== Q1 FY27 BASELINE"
TRANSCRIPT_MARK = f"=== {QUARTER[:2]} FY27 TRANSCRIPT ==="
BASELINE_SECTIONS = ("Verdict", "Promises to track", "Scores")

GOOD = {"saved", "saved_weak_match", "saved_joint", "already_done", "manual_kept", "annual_only",
        "saved_audio", "no_call_held"}
AUDIO_MARK = "Source type: audio"            # header line written by transcribe_audio.py

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
    return qscore(text, CONFIG)


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


def is_audio_file(path):
    return AUDIO_MARK in open(path, encoding="utf-8").read(3000)


# Companies whose record for this quarter is already written: their audio file is never replaced
written = {os.path.basename(p)[:-3] for p in glob.glob(f"data/records/{QUARTER}/**/*.md", recursive=True)}

# Results of the last audio run (transcribe_audio.py): why a recording could not be used
audio_log = {}
if os.path.exists(f"{OUT}/_audio_log.csv"):
    audio_log = {r["code"]: r for r in csv.DictReader(open(f"{OUT}/_audio_log.csv", encoding="utf-8"))
                 if r["status"] != "saved_audio"}

# ---------- candidate calls per company ----------
used_q1 = {source_of(p) for p in glob.glob("data/queue/Q1FY27/*.txt")}

cands, audio_links = {}, {}
for row in csv.DictReader(open(MANIFEST, encoding="utf-8")):
    w = CONFIG["window"]
    if not (w[0] <= row["period"] <= w[1] and row["url"]):
        continue
    if row["status"] == "audio" or is_audio(row["url"]):   # recordings go to transcribe_audio.py
        audio_links.setdefault(row["code"], []).append(row["url"])
    elif row["url"] not in used_q1:
        cands.setdefault(row["code"], []).append(row)
# Screener shows "Transcript" without a link: the call happened, transcript not attached yet
pending = {r["code"] for r in csv.DictReader(open(MANIFEST, encoding="utf-8"))
           if CONFIG["window"][0] <= r["period"] <= CONFIG["window"][1] and not r["url"]
           and r["status"] != "audio"}
if os.path.exists("data/extra_sources.csv"):              # hand-found links
    for r in csv.DictReader(open("data/extra_sources.csv", encoding="utf-8")):
        if r.get("quarter", QUARTER) == QUARTER and r.get("url", "").startswith("http"):
            if is_audio(r["url"]):
                audio_links.setdefault(r["code"].strip(), []).append(r["url"].strip())
                continue
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
    elif os.path.exists(path) and not is_audio_file(path):
        status, source = "already_done", source_of(path)
    elif os.path.exists(path) and (safe(code) in written or code in written or not cands.get(code)):
        status, source = "saved_audio", source_of(path)   # audio kept: record written or no PDF yet
        add_baseline_to_manual(path, code)
    elif code in ANNUAL_ONLY:
        status = "annual_only"
    elif code in NO_CALL:
        status = "no_call_held"
    else:
        audio_path = path if os.path.exists(path) else None
        row, text, status, source = pick(code)
        if row is None and audio_path:                    # no usable PDF yet: keep the audio version
            status, source = "saved_audio", source_of(audio_path)
            add_baseline_to_manual(audio_path, code)
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
        if row is None and status != "saved_audio":
            evidence, source = (f"Screener links: {source}" if source else ""), ""
            if audio_links.get(code):
                evidence = (evidence + " | Audio: " + " ".join(audio_links[code])).strip(" |")
            if code in audio_log:
                evidence = (evidence + f" | Audio {audio_log[code]['status']}: {audio_log[code]['note']}").strip(" |")
        if row is not None:
            if audio_path:
                note = (note + "; " if note else "") + "PDF transcript replaced the earlier audio transcription"
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
order = {"call_done_transcript_pending": 0, "audio_failed": 0, "download_failed": 1, "no_text": 2, "not_quarter_call": 3}
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
