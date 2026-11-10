import requests
from bs4 import BeautifulSoup

url = "https://www.screener.in/company/TATAMOTORS/consolidated/"
r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
print("Status:", r.status_code)

if r.ok:
    soup = BeautifulSoup(r.text, "html.parser")
    print("Page title:", soup.title.get_text(strip=True) if soup.title else "none")
    links = [a for a in soup.find_all("a")
             if a.get_text(strip=True).lower() == "transcript"]
    print("Transcripts found:", len(links))
