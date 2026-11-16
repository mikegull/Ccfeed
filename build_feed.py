#!/usr/bin/env python3
"""
Build an iCalendar (.ics) feed of ALL upcoming Commonwealth Club events,
including venue, by scraping the public listing pages and each event page.

Output:  docs/events.ics   (served by GitHub Pages)
Cache:   cache.json        (event-page details, so each run only fetches new events)
"""

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://www.commonwealthclub.org"
LIST_URL = BASE + "/events?page={n}"
MAX_PAGES = 60
DEFAULT_DURATION = timedelta(minutes=90)
CACHE_REFRESH = timedelta(days=3)      # re-check event pages this often (catches venue/time changes)
REQUEST_DELAY = 1.0                    # seconds between requests, to be polite
PACIFIC = ZoneInfo("America/Los_Angeles")
TZ_ABBR = {
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles", "PT": "America/Los_Angeles",
    "MST": "America/Denver", "MDT": "America/Denver",
    "CST": "America/Chicago", "CDT": "America/Chicago",
    "EST": "America/New_York", "EDT": "America/New_York", "ET": "America/New_York",
}

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "events.ics"
CACHE_FILE = ROOT / "cache.json"

EVENT_HREF = re.compile(r"^/events/(\d{4})-(\d{2})-(\d{2})/[^/?#]+/?$")
# e.g. "Thu, Feb 11 / 11:00 AM PST"
CARD_TIME = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+[A-Z][a-z]{2,8}\.?\s+\d{1,2}\s*/\s*"
    r"(\d{1,2}:\d{2}\s*[AP]\.?M\.?)\s*([A-Z]{2,3})?"
)
TIME_RANGE = re.compile(
    r"(\d{1,2}(?::\d{2})?\s*[AP]\.?M\.?)\s*(?:-|–|—|to)\s*(\d{1,2}(?::\d{2})?\s*[AP]\.?M\.?)\s*([A-Z]{2,3})?",
    re.I,
)
REGIONS = {"San Francisco", "Silicon Valley", "East Bay", "North Bay", "Live Stream",
           "Livestream", "Online", "San Rafael", "San José", "San Jose", "Palo Alto"}

session = requests.Session()
session.headers["User-Agent"] = (
    "Mozilla/5.0 (personal calendar feed builder; contact via GitHub repo) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)


def get(url):
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return r.text
        except requests.RequestException as e:
            print(f"  retry {attempt + 1} for {url}: {e}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    return None


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def parse_clock(s):
    s = s.upper().replace(".", "").replace(" ", "")
    fmt = "%I:%M%p" if ":" in s else "%I%p"
    return datetime.strptime(s, fmt).time()


# ---------------------------------------------------------------- listing pages

def scrape_listing():
    """Return {url: {title, date, time, tz, region}} for every upcoming event."""
    events = {}
    for n in range(MAX_PAGES):
        html = get(LIST_URL.format(n=n))
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")
        new = 0
        for a in soup.find_all("a", href=True):
            href = a["href"].split("#")[0]
            if href.startswith(BASE):
                href = href[len(BASE):]
            m = EVENT_HREF.match(href)
            if not m:
                continue
            url = BASE + href.rstrip("/")
            info = events.setdefault(url, {"date": "-".join(m.groups())})
            text = clean(a.get_text())
            # The longest non-generic link text is the title
            if text and text.lower() not in ("info & tickets", "info", "tickets", "program title") \
                    and len(text) > len(info.get("title", "")):
                info["title"] = text
            if "time" not in info:
                card = a
                for _ in range(6):            # climb to the card containing date/time
                    card = card.parent
                    if card is None:
                        break
                    ctext = clean(card.get_text(" "))
                    tm = CARD_TIME.search(ctext)
                    if tm:
                        info["time"] = tm.group(1)
                        info["tz"] = tm.group(2) or "PST"
                        for r in REGIONS:
                            if r in ctext:
                                info["region"] = r
                                break
                        break
            if url not in seen_this_run:
                seen_this_run.add(url)
                new += 1
        print(f"page {n}: {new} new events")
        if new == 0:
            break
    return events


seen_this_run = set()


# ---------------------------------------------------------------- event pages

def jsonld_event(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                if "@graph" in d:
                    stack.extend(d["@graph"])
                t = d.get("@type")
                if t == "Event" or (isinstance(t, list) and "Event" in t) or str(t).endswith("Event"):
                    return d
            elif isinstance(d, list):
                stack.extend(d)
    return None


def format_location(loc):
    if not loc:
        return ""
    if isinstance(loc, list):
        return "; ".join(filter(None, (format_location(l) for l in loc)))
    if isinstance(loc, str):
        return clean(loc)
    name = clean(loc.get("name", ""))
    addr = loc.get("address", "")
    if isinstance(addr, dict):
        addr = ", ".join(filter(None, [clean(addr.get(k, "")) for k in
                                       ("streetAddress", "addressLocality", "addressRegion", "postalCode")]))
    addr = clean(addr)
    if name and addr and name not in addr:
        return f"{name}, {addr}"
    return name or addr


def find_venue_html(soup):
    """Heuristics for Drupal pages without JSON-LD."""
    # 1. Elements whose class mentions venue/location/address
    for sel in ['[class*="venue"]', '[class*="field--name-field-location"]', '[class*="location"]',
                '[class*="address"]', "address"]:
        for el in soup.select(sel):
            txt = clean(el.get_text(" "))
            if 4 < len(txt) < 250 and not txt.lower().startswith(("all regions", "browse")):
                if re.search(r"\d", txt) or "," in txt or len(txt.split()) >= 2:
                    return re.sub(r"^(Location|Venue|Address)\s*:?\s*", "", txt, flags=re.I)
    # 2. A label like "Location" followed by the value
    for label in soup.find_all(string=re.compile(r"^\s*(Location|Venue|Where)\s*:?\s*$", re.I)):
        nxt = label.find_parent().find_next(string=lambda s: clean(s))
        if nxt:
            txt = clean(nxt)
            if 3 < len(txt) < 250:
                return txt
    # 3. Any street-address-looking line in the main content
    main = soup.find("main") or soup
    m = re.search(r"\d{2,5}\s+[A-Z][\w.\- ]+?(?:Street|St\.?|Avenue|Ave\.?|Blvd\.?|Boulevard|Road|Rd\.?|"
                  r"Way|Embarcadero|Drive|Dr\.?|Place|Pl\.?)\b[^\n]{0,60}?(?:CA|California)(?:\s+\d{5})?",
                  main.get_text("\n"))
    return clean(m.group(0)) if m else ""


def scrape_event(url):
    html = get(url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    ev = jsonld_event(soup)
    if ev:
        out["start_iso"] = ev.get("startDate")
        out["end_iso"] = ev.get("endDate")
        out["venue"] = format_location(ev.get("location"))
        if ev.get("name"):
            out["title"] = clean(ev["name"])
        if ev.get("description"):
            out["description"] = clean(BeautifulSoup(ev["description"], "html.parser").get_text(" "))
    if not out.get("venue"):
        out["venue"] = find_venue_html(soup)
    text = clean((soup.find("main") or soup).get_text(" "))
    tr = TIME_RANGE.search(text)
    if tr:
        out["time_range"] = [tr.group(1), tr.group(2), tr.group(3) or ""]
    if "description" not in out:
        meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", property="og:description")
        if meta and meta.get("content"):
            out["description"] = clean(meta["content"])
    if "title" not in out:
        h1 = soup.find("h1")
        if h1:
            out["title"] = clean(h1.get_text(" "))
    out["livestream"] = bool(re.search(r"live\s?stream|watch online|virtual", text, re.I))
    return out


# ---------------------------------------------------------------- assemble times

def parse_iso(s):
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=PACIFIC)
    except (ValueError, AttributeError):
        return None


def compute_times(item, detail):
    start = parse_iso(detail.get("start_iso") or "")
    end = parse_iso(detail.get("end_iso") or "")
    day = datetime.strptime(item["date"], "%Y-%m-%d").date()
    if not start:
        tzname = TZ_ABBR.get((item.get("tz") or "PST").upper(), "America/Los_Angeles")
        tz = ZoneInfo(tzname)
        clock = item.get("time")
        tr = detail.get("time_range")
        if tr:
            tz = ZoneInfo(TZ_ABBR.get(tr[2].upper(), tzname)) if tr[2] else tz
            clock = clock or tr[0]
        if not clock:
            return day, None, None          # all-day fallback
        start = datetime.combine(day, parse_clock(clock), tz)
        if tr and not end:
            try:
                end = datetime.combine(day, parse_clock(tr[1]), tz)
                if parse_clock(tr[0]) != start.timetz().replace(tzinfo=None):
                    end = None              # range belonged to something else on the page
            except ValueError:
                end = None
    if not end or end <= start:
        end = start + DEFAULT_DURATION
    return day, start, end


# ---------------------------------------------------------------- iCalendar

def esc(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    b = line.encode("utf-8")
    if len(b) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        c = ch.encode("utf-8")
        if len(cur) + len(c) > (75 if not parts else 74):
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += c
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(rows):
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ccfeed//Commonwealth Club Events//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:Commonwealth Club Events",
        "X-WR-CALDESC:All upcoming Commonwealth Club World Affairs events (unofficial)",
        "X-WR-TIMEZONE:America/Los_Angeles",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H", "X-PUBLISHED-TTL:PT12H",
    ]
    for r in rows:
        lines += ["BEGIN:VEVENT",
                  "UID:" + hashlib.sha1(r["url"].encode()).hexdigest() + "@ccfeed",
                  "DTSTAMP:" + now]
        if r["start"]:
            lines += ["DTSTART:" + utc(r["start"]), "DTEND:" + utc(r["end"])]
        else:
            lines += ["DTSTART;VALUE=DATE:" + r["day"].strftime("%Y%m%d"),
                      "DTEND;VALUE=DATE:" + (r["day"] + timedelta(days=1)).strftime("%Y%m%d")]
        lines += ["SUMMARY:" + esc(r["title"]),
                  "LOCATION:" + esc(r["location"]),
                  "URL:" + r["url"],
                  "DESCRIPTION:" + esc((r["description"] + "\n\n" if r["description"] else "")
                                       + "Info & tickets: " + r["url"]),
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


# ---------------------------------------------------------------- main

def main():
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    listing = scrape_listing()
    if not listing:
        print("No events found — site layout may have changed or the site blocked the request. "
              "Keeping the previous feed.", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    rows = []
    for url, item in sorted(listing.items(), key=lambda kv: kv[1]["date"]):
        c = cache.get(url)
        stale = not c or now - datetime.fromisoformat(c["fetched"]) > CACHE_REFRESH
        if stale:
            print("fetching", url)
            detail = scrape_event(url)
            if detail or not c:
                cache[url] = {"fetched": now.isoformat(), "detail": detail}
        detail = cache[url]["detail"]
        day, start, end = compute_times(item, detail)
        venue = detail.get("venue") or ""
        if not venue:
            region = item.get("region", "")
            venue = "Online (livestream)" if region in ("Live Stream", "Livestream", "Online") or \
                (detail.get("livestream") and not region) else region
        rows.append({
            "url": url, "day": day, "start": start, "end": end,
            "title": detail.get("title") or item.get("title") or url.rsplit("/", 1)[-1].replace("-", " ").title(),
            "location": venue, "description": (detail.get("description") or "")[:1500],
        })

    # drop cache entries for events no longer listed
    cache = {u: v for u, v in cache.items() if u in listing}
    CACHE_FILE.write_text(json.dumps(cache, indent=1, sort_keys=True))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build_ics(rows), newline="")
    with_venue = sum(1 for r in rows if r["location"] and r["location"] not in REGIONS)
    print(f"Wrote {len(rows)} events ({with_venue} with a specific venue) to {OUT}")


if __name__ == "__main__":
    main()
