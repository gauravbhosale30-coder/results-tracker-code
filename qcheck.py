"""Quarter check shared by the PDF fetch (fetch_queue.py) and the audio fetch (transcribe_audio.py).

A real Q2 call names Q2/H1 FY27 more often than Q1 FY27. Q1 calls also say "Q2 FY27"
(guidance), so a single mention is not enough: tested on the 468 Q1 transcripts,
"any mention" wrongly passed 83 of them, "Q2 mentions > Q1 mentions" passed 1.
"""
import re

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
    # results_col = the Screener quarterly-results column used by the audio number check.
    "Q2FY27": dict(window=("2026-10", "2026-12"), this=(Q2_RE, CY_RE), prev=Q1_RE,
                   weak_this=WEAK_THIS, weak_prev=WEAK_PREV,
                   waves=("Wave 1", "Wave 2", "Not read"), results_col="Sep 2026"),
}

AUDIO_EXT = re.compile(r"\.(mp3|mp4|m4a|wav|aac|ogg|webm|wma|m3u8)(\?|$)", re.I)


def is_audio(url):
    """A link to a recording rather than a PDF (by file type or a known video host)."""
    return bool(AUDIO_EXT.search(url or "")) or any(h in (url or "") for h in ("youtube.com", "youtu.be"))


def score(text, config):
    """(strength, score): 2 = names the quarter (FY27 or calendar-year wording) more than Q1 FY27;
    1 = never names it but bare Q2/H1 words outnumber bare Q1 words (weak: flagged); 0 = reject."""
    head = text[:20000]
    hits = sum(len(r.findall(head)) for r in config["this"])
    if hits:
        sc = hits - len(config["prev"].findall(head))
        return (2, sc) if sc > 0 else (0, sc)
    a, b = len(config["weak_this"].findall(text)), len(config["weak_prev"].findall(text))
    return (1, a - b) if a >= 3 and a > b else (0, a - b)
