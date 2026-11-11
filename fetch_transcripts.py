import os, re, time
from datetime import date
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

CODES = ["TCS","INFY","HCLTECH","WIPRO","COFORGE","PERSISTENT","HEXT","TATAELXSI",
         "KPITTECH","INTELLECT","NEWGEN","LATENTVIEW","MAPMYINDIA","HAPPSTMNDS",
         "MASTEK","SONATSOFTW"]
MON = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}
today = date.today()
cutoff = (today.year - 2) * 12 + today.month

s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0"
os.makedirs("transcripts", exist_ok=True)
ok = fail = 0

for code in CODES:
    r = s.get(f"https://www.screener.in/company/{code}/consolidated/", timeout=30)
    if not r.ok:
        print(code, "page failed", r.status_code); continue
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a"):
        if a.get_text(strip=True).lower() != "transcript":
            continue
        li = a.find_parent("li")
        m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{4})",
                      li.get_text(" ") if li else "")
        if not m or int(m.group(2)) * 12 + MON[m.group(1)] <= cutoff:
            continue
        url = urljoin(r.url, a.get("href", ""))
        name = f"transcripts/{code}_{m.group(2)}-{MON[m.group(1)]:02d}.pdf"
        try:
            p = s.get(url, timeout=60)
            good = p.ok and p.content[:4] == b"%PDF"
        except Exception as e:
            good, p = False, None
        if good:
            open(name, "wb").write(p.content); ok += 1
        else:
            fail += 1
        print(code, m.group(0), "OK" if good else f"FAILED {p.status_code if p else 'error'}", url)
        time.sleep(2)
    time.sleep(2)

print("Downloaded:", ok, "Failed:", fail)
