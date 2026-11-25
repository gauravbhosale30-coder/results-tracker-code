"""Audio fallback: transcribe call recordings for companies that have NO usable PDF transcript.

Runs on GitHub Actions only (free minutes on this public repo); no Claude tokens are used.

  python transcribe_audio.py plan      decide which companies need audio, split into shards
  python transcribe_audio.py run       transcribe one shard (CODES=...), write to out/
  python transcribe_audio.py collect   copy shard results into data/queue/<QUARTER>/

Which companies: in the tracking list, no file in data/queue/<QUARTER>/ (so no PDF was
saved), and a recording link from Screener ("REC" rows in manifest.csv, status = audio)
or an audio/video link in extra_sources.csv. A PDF always wins: fetch_queue.py replaces
the audio file if a PDF transcript arrives before the company's record is written.

Every transcript goes through the same quarter check as the PDFs (qcheck.py), and then a
number check: revenue, operating profit and net profit for the quarter are taken from
Screener's quarterly results (consolidated and standalone) and looked for in what was said.
Each figure is marked matched / check by hand / not said, so misheard numbers get caught.

Test mode: TEST_URL (+ TEST_CODE, TEST_COL) transcribes one recording with no quarter check
and saves nothing to the data repo; the text and number check are in the run's artifact.
"""
import csv, glob, json, os, re, shutil, subprocess, sys, time
from datetime import datetime, timezone

import requests

from qcheck import CONFIGS, is_audio, score

QUARTER = os.environ.get("QUARTER") or "Q2FY27"
CONFIG = dict(CONFIGS[QUARTER])
LIST = "data/value_chain_leaders.csv"
MANIFEST = "data/manifest.csv"
QUEUE = f"data/queue/{QUARTER}"
AUDIO_LOG = f"{QUEUE}/_audio_log.csv"
OUT = "out"
MODEL = os.environ.get("WHISPER_MODEL") or "large-v3"      # distil-large-v3 garbled figures in the GAIL test
MAX_SHARDS = 20
BUDGET_MIN = float(os.environ.get("BUDGET_MIN") or 300)   # stop starting new files after this
MAX_AUDIO_MIN = 240                                         # longer files are skipped
ANNUAL_ONLY = {"WEALTH"}
# Statuses that will not change by transcribing again: skip that link in later runs
FINAL_FAIL = {"not_quarter_call", "no_speech", "too_long"}
LOG_FIELDS = ["code", "company", "url", "status", "audio_min", "note", "run_date"]

# Spoken forms Whisper writes that the PDF patterns miss ("Q2 of FY27", "first half of FY27")
FYS = r"(FY|F ?by|F5|AY|F\.Y\.|F Y|fiscal( year)?|financial year)"     # how Whisper writes "FY"
AUDIO_THIS = re.compile(r"\b[QH]2,? ?(of )?(the )?" + FYS + r" ?[-']?(20)?27\b|"
                        r"\b(first half|H1) (of )?(the )?(FY|fiscal( year)?|financial year) ?'?(20)?27\b", re.I)
AUDIO_PREV = re.compile(r"\bQ1,? ?(of )?(the )?" + FYS + r" ?[-']?(20)?27\b", re.I)
CONFIG["this"] = tuple(CONFIG["this"]) + (AUDIO_THIS,)
CONFIG["prev"] = re.compile(f"(?:{CONFIG['prev'].pattern})|(?:{AUDIO_PREV.pattern})", re.I)

PROMPT = ("Quarterly earnings conference call of an Indian listed company. Figures in rupees crore. "
          "Q2 FY27, H1 FY27, Q1 FY27, YoY, QoQ, EBITDA, PAT, margin, basis points, order book, capex.")

s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0"


def safe(code):
    return re.sub(r"[^A-Za-z0-9_-]", "_", code)


def read_csv(path):
    return list(csv.DictReader(open(path, encoding="utf-8"))) if os.path.exists(path) else []


# ---------------- which companies need audio ----------------
def audio_links():
    """code -> [(call_month, url)] for recordings inside this quarter's window."""
    w, links = CONFIG["window"], {}
    for r in read_csv(MANIFEST):
        if w[0] <= r["period"] <= w[1] and r["url"] and (r["status"] == "audio" or is_audio(r["url"])):
            links.setdefault(r["code"], []).append((r["period"], r["url"]))
    for r in read_csv("data/extra_sources.csv"):
        if r.get("quarter", QUARTER) == QUARTER and is_audio(r.get("url", "")):
            links.setdefault(r["code"].strip(), []).append(("manual-link", r["url"].strip()))
    return links


def targets(only=None):
    links = audio_links()
    done_fail = {(r["code"], r["url"]) for r in read_csv(AUDIO_LOG) if r["status"] in FINAL_FAIL}
    out = []
    for c in read_csv(LIST):
        code = c["Code"]
        if c["Read wave"] not in CONFIG["waves"] or code in ANNUAL_ONLY:
            continue
        if only and code not in only:
            continue
        if os.path.exists(f"{QUEUE}/{safe(code)}.txt"):        # a transcript is already saved
            continue
        todo = [l for l in links.get(code, []) if (code, l[1]) not in done_fail]
        if todo:
            out.append(code)
    return out


def plan():
    test = os.environ.get("TEST_URL")
    only = {x.strip() for x in (os.environ.get("CODES") or "").split(",") if x.strip()}
    codes = [os.environ.get("TEST_CODE") or "TEST"] if test else targets(only)
    n = min(MAX_SHARDS, len(codes)) or 1
    shards = [{"shard": i, "codes": ",".join(codes[i::n])} for i in range(n)]
    print(f"Companies to transcribe: {len(codes)} in {n} shard(s): {', '.join(codes)}")
    with open(os.environ.get("GITHUB_OUTPUT", "/dev/stdout"), "a") as f:
        f.write(f"count={len(codes)}\nshards={json.dumps(shards)}\n")


# ---------------- download + transcribe ----------------
def ca_bundle_for(url):
    """Some company sites send an incomplete certificate chain (browsers fetch the missing
    intermediate themselves, Python does not). Fetch it the same way (AIA) and verify with it."""
    try:
        import certifi
        from aia import AIASession
        chain = AIASession().cadata_from_url(url)
    except Exception:
        return None
    path = "/tmp/ca_bundle.pem"
    with open(path, "w") as f:
        f.write(open(certifi.where()).read() + "\n" + chain)
    return path


def download(url, dest):
    """Direct media file first; web pages (YouTube, webcast players) through yt-dlp."""
    err = download_once(url, dest, True)
    if err and "SSL" in err:
        bundle = ca_bundle_for(url)
        if bundle:
            err = download_once(url, dest, bundle)
    return err


def download_once(url, dest, verify):
    try:
        with s.get(url, stream=True, timeout=(30, 300), verify=verify) as r:
            ctype = r.headers.get("content-type", "")
            if r.ok and ("audio" in ctype or "video" in ctype or "octet-stream" in ctype
                         or (is_audio(url) and "html" not in ctype)):
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                if os.path.getsize(dest) > 100_000:
                    return None
            elif not r.ok and "html" not in ctype:
                return f"HTTP {r.status_code}"
    except requests.RequestException as e:
        err = type(e).__name__
    else:
        err = "not a media file"
    out = subprocess.run(["yt-dlp", "-q", "--no-playlist", "-f", "bestaudio/best", "-o", dest + ".%(ext)s", url],
                         capture_output=True, text=True, timeout=1800)
    got = glob.glob(dest + ".*")
    if out.returncode == 0 and got:
        shutil.move(got[0], dest)
        return None
    last = (out.stderr or "").strip().splitlines()
    return f"{err}; yt-dlp: {last[-1] if last else 'failed'}"


def to_wav(src, wav):
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", src, "-vn",
                    "-ac", "1", "-ar", "16000", wav], check=True)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", wav], capture_output=True, text=True)
    return float(out.stdout.strip() or 0) / 60


_model = None


def transcribe(wav):
    global _model
    from faster_whisper import WhisperModel
    if _model is None:
        _model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
    import numpy as np                     # decode with ffmpeg: faster-whisper's own decoder (PyAV) breaks across versions
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", wav, "-f", "f32le",
                          "-ac", "1", "-ar", "16000", "-"], capture_output=True, check=True).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    segs, _ = _model.transcribe(audio, language="en", beam_size=5, vad_filter=True,
                                initial_prompt=PROMPT, condition_on_previous_text=False)
    paras, cur, last_end = [], [], None
    for sg in segs:
        if cur and last_end is not None and sg.start - last_end > 1.5:   # pause = new paragraph
            paras.append(cur)
            cur = []
        if not cur:
            m, sec = divmod(int(sg.start), 60)
            cur.append(f"[{m // 60}:{m % 60:02d}:{sec:02d}]")
        cur.append(sg.text.strip())
        last_end = sg.end
    if cur:
        paras.append(cur)
    return "\n\n".join(" ".join(p) for p in paras)


# ---------------- number check ----------------
# Reported rows on Screener's quarterly table, and the words used for them on a call
# (incl. Whisper's usual mishearings: EBITDA -> "Vita", PBT -> "PVT").
METRICS = [
    ("Revenue", re.compile(r"^(Sales|Revenue)", re.I),
     re.compile(r"revenue|income from operations|turnover|top ?line|\bsales\b|total income|net interest income|\bNII\b", re.I)),
    ("Operating profit / EBITDA", re.compile(r"^(Operating Profit|Financing Profit)", re.I),
     re.compile(r"EBITDA|E?B?IT ?DA|\bVita\b|operating profit|operating income|pre[- ]provision|\bPPOP\b|\bEBIT\b", re.I)),
    ("Profit before tax", re.compile(r"^Profit before tax", re.I),
     re.compile(r"\bPBT\b|\bPVT\b|profit before tax", re.I)),
    ("Net profit", re.compile(r"^Net Profit", re.I),
     re.compile(r"\bPAT\b|net profit|profit after tax|bottom ?line|net income|profit for the (quarter|period)|minority interest", re.I)),
]
NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(lakh crores?|lac crores?|thousand crores?|crores?|crs?\b|cr\.|billion|bn\b|"
                 r"million|mn\b|lakhs?\b|lacs?\b)", re.I)
VALID = re.compile(r"^(\d{1,3}(,\d{3})+|\d{1,2}(,\d{2})*,\d{3}|\d+)(\.\d+)?$")   # 1,234,567 / 12,34,567 / 1234
UNIT = [("lakh cr", 1e5), ("lac cr", 1e5), ("thousand", 1e3), ("cr", 1), ("billion", 100), ("bn", 100),
        ("million", 0.1), ("mn", 0.1), ("lakh", 0.01), ("lac", 0.01)]
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def to_crore(num, unit):
    v, u = float(num.replace(",", "")), unit.lower()
    for k, f in UNIT:
        if u.startswith(k):
            return v * f
    return None


def periods(col):
    """This quarter, the quarter before, the same quarter last year ("Sep 2026" -> Jun 2026, Sep 2025)."""
    m, y = col.split()
    i = MON.index(m)
    prev = f"{MON[(i - 3) % 12]} {y if i >= 3 else int(y) - 1}"
    return [("this quarter", col), ("previous quarter", prev), ("same quarter last year", f"{m} {int(y) - 1}")]


def screener_results(code, cols):
    """{column: {'Consolidated'|'Standalone': {row label: value in Rs cr}}}."""
    from bs4 import BeautifulSoup
    res = {}
    for kind, path in (("Consolidated", "consolidated/"), ("Standalone", "")):
        try:
            r = s.get(f"https://www.screener.in/company/{code}/{path}", timeout=30)
        except requests.RequestException:
            continue
        time.sleep(2)
        sec = BeautifulSoup(r.text, "html.parser").find("section", id="quarters") if r.ok else None
        table = sec.find("table") if sec else None
        if not table:
            continue
        heads = [th.get_text(strip=True) for th in table.find_all("th")]
        for col in cols:
            if col not in heads:
                continue
            i = heads.index(col)
            for tr in table.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if len(cells) > i:
                    try:
                        v = float(cells[i].replace(",", "").replace("%", ""))
                    except ValueError:
                        continue
                    res.setdefault(col, {}).setdefault(kind, {})[cells[0].replace("+", "").strip()] = v
    return res


def stamp(text, pos):
    m = None
    for m in re.finditer(r"\[\d+:\d\d:\d\d\]", text[:pos]):
        pass
    return m.group(0) if m else ""


def number_check(code, text, col):
    """Lines for the queue file and a one-line summary."""
    pers = periods(col)
    reported = screener_results(code, [c for _, c in pers])
    lines = [f"=== NUMBER CHECK: recording vs reported results (Screener, Rs cr) ===",
             "MATCHED = a figure said near the item is within 1.5% of the reported one. "
             "CHECK BY HAND = the nearest figure said is 1.5-35% off (misheard, or a segment / "
             "standalone / adjusted figure). NOT SAID = no figure found for it."]
    found = [(m.start(), to_crore(m.group(1), m.group(2)), m.group(0).strip()) for m in NUM.finditer(text)]
    found = [f for f in found if f[1]]
    tally = {"matched": 0, "check by hand": 0, "not said": 0}
    if not reported:
        lines.append("Reported results not found on Screener: check every figure by hand.")
    for label, c in pers:
        if c not in reported:
            continue
        lines.append(f"{label.capitalize()} ({c}):")
        for name, row_re, kw in METRICS:
            values = {k: v for k, rows in reported[c].items() for lab, v in rows.items()
                      if row_re.match(lab) and abs(v) >= 1}          # zero rows (e.g. REITs) cannot be checked
            if not values:
                continue
            near = []
            for m in kw.finditer(text):
                near += [f for f in found if m.start() <= f[0] <= m.start() + 220]
            best = None                   # (gap, said, basis, reported, position)
            for basis, rv in values.items():
                for pos, cr, said in near:
                    gap = abs(cr - rv) / abs(rv)
                    if best is None or gap < best[0]:
                        best = (gap, said, basis, rv, pos)
                for pos, cr, said in found:   # the exact figure said anywhere
                    gap = abs(cr - rv) / abs(rv)
                    if gap <= 0.01 and (best is None or gap < best[0]):
                        best = (gap, said, basis, rv, pos)
            rep = ", ".join(f"{k.lower()} {v:,.0f}" for k, v in values.items())
            if best and best[0] <= 0.015:
                verdict, detail = "matched", f'said "{best[1]}" {stamp(text, best[4])} vs {best[2].lower()} {best[3]:,.0f}'
            elif best and best[0] <= 0.35:
                verdict, detail = "check by hand", (f'nearest said "{best[1]}" {stamp(text, best[4])} '
                                                    f'({best[0]:.0%} off) vs reported {rep}')
            else:
                verdict, detail = "not said", f"reported {rep}"
            if label == "this quarter":
                tally[verdict] += 1
            lines.append(f"- {name}: {verdict.upper()} ({detail})")
    # Figures Whisper probably garbled: impossible digit grouping, or far out of scale for the company
    scale = max([v for c in reported.values() for rows in c.values() for lab, v in rows.items()
                 if re.match(r"^(Sales|Revenue)", lab)] or [0])
    odd = []
    for m in NUM.finditer(text):
        cr = to_crore(m.group(1), m.group(2))
        if not VALID.match(m.group(1).rstrip(".")) or (scale and cr and cr > 50 * scale):
            odd.append(f'"{m.group(0).strip()}" {stamp(text, m.start())}')
    if odd:
        lines.append(f"Possibly misheard figures (odd format or far out of scale), check against the "
                     f"results release: {'; '.join(odd[:20])}")
    summary = ", ".join(f"{v} {k}" for k, v in tally.items()) + f", {len(odd)} odd figure(s)"
    return "\n".join(lines) + "\n\n", f"number check this quarter: {summary}"


# ---------------- run one shard ----------------
def header(c, call_month, url, note):
    return (f"Company: {c['Company']}\nCode: {c['Code']}\nValue chain: {c['Value chain']}\n"
            f"Stage: {c['Stage']}\nSub-industry: {c['Sub-industry']}\nRead wave: {c['Read wave']}\n"
            f"Quarter: {QUARTER}\nCall month: {call_month}\nSource: {url}\n"
            f"Source type: audio transcription (Whisper {MODEL}; no speaker names; check figures)\n"
            f"Note: {note}\n\n")


def run():
    start = time.time()
    os.makedirs(f"{OUT}/queue", exist_ok=True)
    test = os.environ.get("TEST_URL")
    codes = [x for x in (os.environ.get("CODES") or "").split(",") if x]
    companies = {c["Code"]: c for c in read_csv(LIST)}
    links = audio_links()
    done_fail = {(r["code"], r["url"]) for r in read_csv(AUDIO_LOG) if r["status"] in FINAL_FAIL}
    log = []
    for code in codes:
        c = companies.get(code, {"Company": code, "Code": code, "Value chain": "", "Stage": "",
                                 "Sub-industry": "", "Read wave": ""})
        todo = [("test", test)] if test else [l for l in links.get(code, []) if (code, l[1]) not in done_fail]
        for call_month, url in todo:
            row = {"code": code, "company": c["Company"], "url": url, "audio_min": "", "note": "",
                   "run_date": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
            log.append(row)
            if (time.time() - start) / 60 > BUDGET_MIN:
                row["status"], row["note"] = "over_time", "run time used up; next run retries"
                continue
            src, wav = f"/tmp/{safe(code)}.media", f"/tmp/{safe(code)}.wav"
            err = download(url, src)
            if err:
                row["status"], row["note"] = "download_failed", err[:200]
                print(f"{code}: download failed: {err}", flush=True)
                continue
            try:
                mins = to_wav(src, wav)
            except subprocess.CalledProcessError:
                row["status"], row["note"] = "download_failed", "file is not playable audio/video"
                continue
            row["audio_min"] = f"{mins:.0f}"
            if mins > MAX_AUDIO_MIN:
                row["status"], row["note"] = "too_long", f"{mins:.0f} min recording"
                continue
            t0 = time.time()
            try:
                text = transcribe(wav)
            except Exception as e:                        # one bad file must not stop the shard
                row["status"], row["note"] = "transcribe_failed", f"{type(e).__name__}: {e}"[:200]
                print(f"{code}: transcription failed: {e}", flush=True)
                continue
            took = (time.time() - t0) / 60
            print(f"{code}: {mins:.0f} min audio transcribed in {took:.0f} min, {len(text)} chars", flush=True)
            if len(text) < 3000:
                row["status"], row["note"] = "no_speech", f"only {len(text)} characters of speech"
                continue
            strength, sc = (2, 0) if test else score(text, CONFIG)
            if not strength:
                row["status"], row["note"] = "not_quarter_call", f"quarter check score {sc}"
                with open(f"{OUT}/queue/_rejected_{safe(code)}.txt", "w", encoding="utf-8") as f:
                    f.write(f"Code: {code}\nSource: {url}\n\n{text}")
                continue
            col = os.environ.get("TEST_COL") or CONFIG["results_col"]
            try:
                checks, summary = number_check(code, text, col)
            except Exception as e:                        # never lose a finished transcript
                checks = f"=== NUMBER CHECK ===\nNumber check failed ({type(e).__name__}): check figures by hand.\n\n"
                summary = "number check failed: check figures by hand"
            note = f"Audio transcription ({mins:.0f} min); {summary}"
            if strength == 1:
                note += "; weak quarter match: confirm the call date before reading"
            row["status"], row["note"] = "saved_audio", summary
            with open(f"{OUT}/queue/{safe(code)}.txt", "w", encoding="utf-8") as f:
                f.write(header(c, call_month, url, note))
                f.write(checks + "=== AUDIO TRANSCRIPT ([h:mm:ss] = time in the recording) ===\n\n" + text + "\n")
            if test:                                  # test: show the result in the run log
                print("=====TEST OUTPUT START=====\n" + open(f"{OUT}/queue/{safe(code)}.txt", encoding="utf-8").read()
                      + "\n=====TEST OUTPUT END=====", flush=True)
            break                                     # one good call per company is enough
    with open(f"{OUT}/audio_log_{os.environ.get('SHARD', '0')}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        w.writeheader()
        w.writerows(log)


# ---------------- collect shard results into the data repo ----------------
def collect():
    os.makedirs(f"{QUEUE}/_rejected", exist_ok=True)
    saved = 0
    for p in sorted(set(glob.glob(f"{OUT}/**/queue/*.txt", recursive=True))):
        name = os.path.basename(p)
        if name.startswith("_rejected_"):
            shutil.copy(p, f"{QUEUE}/_rejected/audio_{name[len('_rejected_'):]}")
        elif not os.path.exists(f"{QUEUE}/{name}"):   # never overwrite a transcript saved meanwhile
            shutil.copy(p, f"{QUEUE}/{name}")
            saved += 1
    new = []
    for p in sorted(set(glob.glob(f"{OUT}/**/audio_log_*.csv", recursive=True))):
        new += read_csv(p)
    keys = {(r["code"], r["url"]) for r in new}
    merged = [r for r in read_csv(AUDIO_LOG) if (r["code"], r["url"]) not in keys] + new
    with open(AUDIO_LOG, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        w.writeheader()
        w.writerows(sorted(merged, key=lambda r: r["code"]))
    counts = {}
    for r in new:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"Audio transcripts saved: {saved}. This run: {counts}")


if __name__ == "__main__":
    {"plan": plan, "run": run, "collect": collect}[sys.argv[1]]()
