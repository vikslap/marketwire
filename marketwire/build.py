"""MarketWire builder.

Pulls market news RSS feeds for India and the US, tags each story with the
listed companies it mentions, merges it with what is already published, and
writes site/news.json for the static page to read.

Run locally:  python build.py
Env vars:
  SITE_URL   URL of the published site (to keep older stories between runs)
  KEEP_HOURS how long to keep a story (default 72)
"""
import csv
import html
import io
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).parent
SITE = ROOT / "site"
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
KEEP_HOURS = int(os.environ.get("KEEP_HOURS", "72"))
MAX_ITEMS = 2500
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"

NSE_LIST_URLS = [
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
]
SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

# Words that look like tickers or company names but usually are not.
NOT_TICKERS = {
    "IPO", "CEO", "CFO", "GDP", "RBI", "SEBI", "NSE", "BSE", "FII", "FPI", "DII", "USA", "US",
    "UK", "EU", "AI", "EV", "IT", "FY", "QOQ", "YOY", "EPS", "PE", "MSCI", "ETF", "NAV", "SIP",
    "GST", "CPI", "WPI", "IMF", "FED", "FOMC", "OPEC", "SEC", "NYSE", "PSU", "PSB", "MF",
    "NBFC", "INR", "USD", "THE", "AND", "FOR", "NEW", "BIG", "TOP", "NOW", "ALL", "ONE",
    "GOLD", "OIL", "BUY", "SELL", "HOLD", "LIVE", "NEWS", "WATCH", "IST", "EST", "PM", "AM",
    "MD", "Q1", "Q2", "Q3", "Q4", "H1", "H2", "TV", "UPI", "ITR", "TDS", "LTCG", "STCG",
    "ESG", "ATM", "OFS", "QIP", "NCD", "REIT", "INVIT", "F&O", "FNO", "VIX", "ICICI", "HDFC",
    "NIFTY", "SENSEX", "BANK", "INDIA", "POWER", "STEEL", "AUTO",
}
# Official names that are plain English words; only matched via aliases.
NOT_NAMES = {
    "Target", "Ball", "Equity", "Gold", "Global", "Focus", "Future", "Vision", "Apex", "Trust",
    "Unity", "Prime", "Life", "Mint", "Ideal", "Star", "Sun", "Crown", "Alpha", "Delta", "Omega",
    "Fortune", "Pioneer", "Premier", "National", "General", "United", "Universal", "Standard",
    "Progressive", "Southern", "Public", "Allied", "Bharat", "India", "Indian", "Hindustan",
}
SUFFIX = re.compile(
    r"[\s,]+(limited|ltd\.?|inc\.?|incorporated|corp\.?|corporation|company|co\.?|plc|"
    r"holdings?|group|the|\(the\)|class [a-c])$",
    re.I,
)


def log(*a):
    print(*a, flush=True)


def get(url, timeout=20):
    r = requests.get(url, headers={"User-Agent": UA, "Accept": "*/*"}, timeout=timeout)
    r.raise_for_status()
    return r


def previous_json(name):
    if not SITE_URL:
        return None
    try:
        return get(f"{SITE_URL}/{name}?t={int(time.time())}").json()
    except Exception as e:  # first run, or site not up yet
        log(f"  no previous {name}: {e}")
        return None


# ---------- company lists ----------

def clean_name(name):
    name = html.unescape(name).strip()
    for _ in range(3):
        name = SUFFIX.sub("", name).strip(" ,.")
    return name


def load_companies():
    """Return {key: {"sym","market","name"}} for NSE + S&P 500, with cache fallback."""
    companies = {}

    nse = None
    for url in NSE_LIST_URLS:
        try:
            nse = get(url).text
            break
        except Exception as e:
            log(f"  NSE list failed ({url}): {e}")
    if nse:
        for row in csv.DictReader(io.StringIO(nse)):
            row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
            sym, name = row.get("SYMBOL"), row.get("NAME OF COMPANY")
            if sym and name:
                companies[f"IN:{sym}"] = {"sym": sym, "market": "IN", "name": name}

    try:
        for row in csv.DictReader(io.StringIO(get(SP500_URL).text)):
            sym = row["Symbol"].strip()
            companies[f"US:{sym}"] = {"sym": sym, "market": "US", "name": row["Security"].strip()}
    except Exception as e:
        log(f"  S&P 500 list failed: {e}")

    have_in = any(k.startswith("IN:") for k in companies)
    have_us = any(k.startswith("US:") for k in companies)
    if not (have_in and have_us):
        cached = previous_json("companies.json") or {}
        for k, v in cached.items():
            if (k.startswith("IN:") and not have_in) or (k.startswith("US:") and not have_us):
                companies.setdefault(k, v)
        log(f"  used cached company list to fill gaps")
    log(f"  companies: {sum(k.startswith('IN:') for k in companies)} IN, "
        f"{sum(k.startswith('US:') for k in companies)} US")
    return companies


def build_matchers(companies, aliases):
    """Map phrase -> set of keys; plus symbol lookup for ticker-style mentions."""
    phrases = {}

    def add(phrase, key):
        phrase = phrase.strip()
        if len(phrase) >= 2:
            phrases.setdefault(phrase, set()).add(key)

    for key, c in companies.items():
        n = clean_name(c["name"])
        if len(n) >= 4 and n not in NOT_NAMES and not n.isdigit():
            add(n, key)
    for key, names in aliases.items():
        if key.startswith("_"):
            continue
        sym = key.split(":", 1)[1]
        companies.setdefault(key, {"sym": sym, "market": key[:2], "name": names[0]})
        for n in names:
            add(n, key)

    # Longest phrases first so "Tata Motors" wins over "Tata".
    ordered = sorted(phrases, key=len, reverse=True)
    chunks = []
    for i in range(0, len(ordered), 400):  # keep each regex a sane size
        part = "|".join(re.escape(p) for p in ordered[i:i + 400])
        chunks.append(re.compile(rf"(?<![\w&]){'(?:' + part + ')'}(?![\w&])"))

    in_syms = {c["sym"]: k for k, c in companies.items()
               if c["market"] == "IN" and len(c["sym"]) >= 3 and c["sym"] not in NOT_TICKERS}
    us_syms = {c["sym"]: k for k, c in companies.items() if c["market"] == "US"}
    return phrases, chunks, in_syms, us_syms


US_TICKER = re.compile(r"(?:\$|\b(?:NYSE|NASDAQ|Nasdaq|NYSEARCA|AMEX)\s?:\s?|\()([A-Z]{1,5}(?:\.[A-B])?)\)?")
CAPS_WORD = re.compile(r"(?<![\w&$])([A-Z][A-Z0-9&\-]{2,14})(?![\w&])")


def tag(text, matchers):
    phrases, chunks, in_syms, us_syms = matchers
    found = set()
    for rx in chunks:
        for m in rx.finditer(text):
            found |= phrases.get(m.group(0), set())
    for m in US_TICKER.finditer(text):
        sym = m.group(1)
        k = us_syms.get(sym)
        if not k or sym in NOT_TICKERS:
            continue
        if m.group(0).startswith("(") and (len(sym) < 2 or not m.group(0).endswith(")")):
            continue  # "(A)" style is too ambiguous
        found.add(k)
    for m in CAPS_WORD.finditer(text):
        w = m.group(1)
        if w in in_syms and w not in NOT_TICKERS:
            found.add(in_syms[w])
    return sorted(found)


# ---------- feeds ----------

def to_iso(entry):
    for f in ("published_parsed", "updated_parsed"):
        t = entry.get(f)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
    for f in ("published", "updated"):
        s = entry.get(f)
        if s:
            try:
                return parsedate_to_datetime(s).astimezone(timezone.utc).isoformat()
            except Exception:
                pass
    return datetime.now(timezone.utc).isoformat()


TAGS = re.compile(r"<[^>]+>")


def strip_html(s):
    s = html.unescape(TAGS.sub(" ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def fetch(src):
    try:
        feed = feedparser.parse(get(src["url"]).content)
        items = []
        for e in feed.entries:
            title = strip_html(e.get("title"))
            link = e.get("link") or ""
            if not title or not link:
                continue
            publisher = None
            if "news.google.com" in src["url"]:
                # Google News titles end with " - Publisher"
                if " - " in title:
                    title, publisher = title.rsplit(" - ", 1)
            summary = strip_html(e.get("summary"))[:280]
            if "news.google.com" in src["url"]:
                summary = ""
            items.append({
                "title": title,
                "link": link,
                "summary": summary,
                "source": publisher or src["name"],
                "market": src["market"],
                "time": to_iso(e),
            })
        return src["name"], items, None
    except Exception as e:
        return src["name"], [], str(e)[:120]


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def main():
    SITE.mkdir(exist_ok=True)
    feeds = json.loads((ROOT / "feeds.json").read_text())["sources"]
    aliases = json.loads((ROOT / "aliases.json").read_text())

    log("Loading company lists")
    companies = load_companies()
    matchers = build_matchers(companies, aliases)

    log(f"Fetching {len(feeds)} feeds")
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(fetch, feeds))
    health = []
    fresh = []
    for name, items, err in results:
        health.append({"name": name, "ok": err is None, "count": len(items), "error": err})
        log(f"  {'ok ' if err is None else 'ERR'} {name}: {len(items)} {err or ''}")
        fresh.extend(items)

    old = (previous_json("news.json") or {}).get("items", [])
    now = datetime.now(timezone.utc)
    seen_links, seen_titles, out = set(), set(), []
    # Fresh first so updated metadata wins; old ones keep their first-seen time.
    old_first_seen = {i["link"]: i.get("seen") for i in old}
    for it in fresh + old:
        nt = norm_title(it["title"])
        if it["link"] in seen_links or nt in seen_titles:
            continue
        try:
            t = datetime.fromisoformat(it["time"])
        except Exception:
            continue
        if t > now:  # some feeds publish future timestamps
            t = now
            it["time"] = now.isoformat()
        if (now - t).total_seconds() > KEEP_HOURS * 3600:
            continue
        seen_links.add(it["link"])
        seen_titles.add(nt)
        it["seen"] = old_first_seen.get(it["link"]) or it.get("seen") or now.isoformat()
        it["tickers"] = tag(f"{it['title']} {it['summary']}", matchers)
        out.append(it)

    out.sort(key=lambda i: i["time"], reverse=True)
    out = out[:MAX_ITEMS]

    used = {k for i in out for k in i["tickers"]}
    names = {k: companies[k]["name"] for k in used if k in companies}

    payload = {
        "updated": now.isoformat(),
        "count": len(out),
        "feeds": health,
        "names": names,
        "items": out,
    }
    (SITE / "news.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    # Cache the company list so a later run can survive NSE blocking the download.
    base = {k: v for k, v in companies.items() if not k.startswith("_")}
    (SITE / "companies.json").write_text(json.dumps(base, ensure_ascii=False, separators=(",", ":")))
    tagged = sum(1 for i in out if i["tickers"])
    log(f"Wrote {len(out)} stories ({tagged} tagged to companies)")


if __name__ == "__main__":
    main()
