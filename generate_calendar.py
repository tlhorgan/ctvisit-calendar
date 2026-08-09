from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from icalendar import Calendar, Event

BASE = "https://ctvisit.com"
DISCOVERY_URL = f"{BASE}/upcoming-events"
OUTPUT = Path("ctvisit.ics")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CTvisitCalendar/3.0)"
}

MONTHS = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December|"
    "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)

def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()

def get(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response.text

def discover_event_urls() -> list[str]:
    html = get(DISCOVERY_URL)
    found = re.findall(r'href=["\']([^"\']*/events/[^"\'?#]+)', html, flags=re.I)

    urls = set()
    for href in found:
        url = urljoin(BASE, href).rstrip("/")
        if url != f"{BASE}/events":
            urls.add(url)

    urls = sorted(urls)
    print(f"Found {len(urls)} event URLs")
    for url in urls[:20]:
        print(f"  discovered: {url}")
    return urls

def extract_description(soup: BeautifulSoup) -> str:
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return clean(tag["content"])
    return ""

def extract_location(soup: BeautifulSoup) -> str:
    lines = [clean(x) for x in soup.get_text("\n", strip=True).splitlines() if clean(x)]
    for i, line in enumerate(lines):
        if line.lower() == "location":
            values = []
            for nxt in lines[i + 1:i + 6]:
                low = nxt.lower()
                if low in {"contact", "times", "admission", "how to get here"}:
                    break
                if "map & directions" in low:
                    break
                values.append(nxt)
            return ", ".join(values)
    return ""

def parse_happens_dates(soup: BeautifulSoup):
    text = soup.get_text("\n", strip=True)
    lines = [clean(x) for x in text.splitlines() if clean(x)]

    pattern = re.compile(
        rf"\b({MONTHS})\s+(\d{{1,2}}),\s+(\d{{4}}),\s*"
        r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))"
        r"(?:\s+to\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)))?",
        re.I,
    )

    occurrences = []
    for line in lines:
        for m in pattern.finditer(line):
            try:
                d = dtparser.parse(f"{m.group(1)} {m.group(2)}, {m.group(3)}").date()
                start_t = dtparser.parse(m.group(4)).time()
                start_dt = datetime.combine(d, start_t)

                if m.group(5):
                    end_t = dtparser.parse(m.group(5)).time()
                    end_dt = datetime.combine(d, end_t)
                    if end_dt <= start_dt:
                        end_dt += timedelta(days=1)
                else:
                    end_dt = start_dt + timedelta(hours=2)

                occurrences.append(("timed", start_dt, end_dt))
            except Exception:
                pass

    seen = set()
    result = []
    for item in occurrences:
        key = (item[1], item[2])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result

def parse_date_range_fallback(soup: BeautifulSoup):
    text = clean(soup.get_text(" ", strip=True))
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", text)]
    year = years[0] if years else datetime.now().year

    full_range = re.search(
        rf"\b({MONTHS})\s+(\d{{1,2}}),?\s+(20\d{{2}})\s*[–—-]\s*"
        rf"({MONTHS})\s+(\d{{1,2}}),?\s*(20\d{{2}})?",
        text,
        re.I,
    )
    if full_range:
        try:
            start = dtparser.parse(
                f"{full_range.group(1)} {full_range.group(2)}, {full_range.group(3)}"
            ).date()
            end_year = full_range.group(6) or full_range.group(3)
            end = dtparser.parse(
                f"{full_range.group(4)} {full_range.group(5)}, {end_year}"
            ).date()
            return [("allday", start, end + timedelta(days=1))]
        except Exception:
            pass

    single = re.search(
        rf"\b({MONTHS})\s+(\d{{1,2}}),?\s+(20\d{{2}})\b",
        text,
        re.I,
    )
    if single:
        try:
            d = dtparser.parse(
                f"{single.group(1)} {single.group(2)}, {single.group(3)}"
            ).date()
            return [("allday", d, d + timedelta(days=1))]
        except Exception:
            pass

    return []

def scrape_event(url: str):
    soup = BeautifulSoup(get(url), "html.parser")

    h1 = soup.find("h1")
    title = clean(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        return []

    occurrences = parse_happens_dates(soup)
    if not occurrences:
        occurrences = parse_date_range_fallback(soup)
    if not occurrences:
        return []

    location = extract_location(soup)
    description = extract_description(soup)

    return [
        {
            "title": title,
            "start": start,
            "end": end,
            "location": location,
            "description": description,
            "url": url,
        }
        for _, start, end in occurrences
    ]

def build_calendar(items):
    cal = Calendar()
    cal.add("prodid", "-//CTvisit Events Calendar//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Connecticut Events")
    cal.add("x-wr-timezone", "America/New_York")

    seen = set()
    for item in items:
        key = (item["url"], str(item["start"]))
        if key in seen:
            continue
        seen.add(key)

        ev = Event()
        uid_source = f"{item['url']}|{item['start']}"
        ev.add("uid", hashlib.sha256(uid_source.encode()).hexdigest()[:28] + "@ctvisit-calendar")
        ev.add("dtstamp", datetime.utcnow())
        ev.add("summary", item["title"])
        ev.add("dtstart", item["start"])
        ev.add("dtend", item["end"])
        ev.add("url", item["url"])

        if item["location"]:
            ev.add("location", item["location"])

        desc = item["description"]
        if desc:
            desc += "\n\n"
        desc += f"Source: {item['url']}"
        ev.add("description", desc)

        cal.add_component(ev)

    OUTPUT.write_bytes(cal.to_ical())
    return len(seen)

def main():
    urls = discover_event_urls()

    all_events = []
    skipped = 0

    for i, url in enumerate(urls, 1):
        try:
            events = scrape_event(url)
            if events:
                all_events.extend(events)
                print(f"[{i}/{len(urls)}] {events[0]['title']} ({len(events)} occurrence(s))")
            else:
                skipped += 1
                print(f"[{i}/{len(urls)}] SKIPPED {url}: no usable date found")
        except Exception as exc:
            skipped += 1
            print(f"[{i}/{len(urls)}] ERROR {url}: {exc}")

    all_events.sort(key=lambda x: (str(x["start"]), x["title"]))
    count = build_calendar(all_events)

    print(f"Skipped {skipped} event pages")
    print(f"Wrote {OUTPUT} with {count} calendar events")

    if len(urls) < 10:
        raise RuntimeError(
            f"Discovery only found {len(urls)} event URLs; refusing to publish a bad feed."
        )
    if count < 10:
        raise RuntimeError(
            f"Only generated {count} calendar events; refusing to publish a bad feed."
        )

if __name__ == "__main__":
    main()
