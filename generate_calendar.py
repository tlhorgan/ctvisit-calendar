from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from icalendar import Calendar, Event
from playwright.sync_api import sync_playwright

BASE = "https://ctvisit.com"
CALENDAR_URL = f"{BASE}/upcoming-events"
OUTPUT = Path("ctvisit.ics")

def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()

def discover_event_urls(page) -> list[str]:
    page.goto(CALENDAR_URL, wait_until="networkidle", timeout=90000)
    page.wait_for_timeout(2000)

    urls = set()
    previous_count = 0
    stable_rounds = 0

    for _ in range(60):
        # CTvisit's /events page is primarily a JS search shell.  The
        # /upcoming-events page server-renders many real event cards, so use
        # those links as the discovery source.
        hrefs = page.locator('a[href*="/events/"]').evaluate_all(
            "(els) => els.map(e => e.href)"
        )
        for href in hrefs:
            parsed = urlparse(href)
            if parsed.netloc.endswith("ctvisit.com"):
                path = parsed.path.rstrip("/")
                if path.startswith("/events/") and path != "/events":
                    urls.add(href.split("?")[0].split("#")[0])

        clicked = False
        for label in ["Load More", "Show More", "More Events", "Next"]:
            loc = page.get_by_text(label, exact=False)
            if loc.count() and loc.first.is_visible():
                try:
                    loc.first.click(timeout=1500)
                    page.wait_for_timeout(1200)
                    clicked = True
                    break
                except Exception:
                    pass

        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(600)

        if len(urls) == previous_count and not clicked:
            stable_rounds += 1
        else:
            stable_rounds = 0
        previous_count = len(urls)

        if stable_rounds >= 8:
            break

    return sorted(urls)

def extract_description(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return clean(meta["content"])
    meta = soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        return clean(meta["content"])
    return ""

def parse_occurrence(line: str):
    """
    Expected examples:
      Jun 25, 2026, 7:30pm to 9:00pm Timezone: Eastern Time (US & Canada)
      Sep 12, 2026, 10:00am
    """
    line = clean(line)
    line = re.sub(r"\s*Timezone:.*$", "", line, flags=re.I)
    m = re.search(
        r"([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}),\s*"
        r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))"
        r"(?:\s+to\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)))?",
        line,
        re.I,
    )
    if not m:
        return None

    d = dtparser.parse(m.group(1)).date()
    start_t = dtparser.parse(m.group(2)).time()
    start_dt = datetime.combine(d, start_t)

    if m.group(3):
        end_t = dtparser.parse(m.group(3)).time()
        end_dt = datetime.combine(d, end_t)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
    else:
        end_dt = start_dt + timedelta(hours=2)

    return start_dt, end_dt

def parse_occurrences(soup: BeautifulSoup) -> list[tuple[datetime, datetime]]:
    text = soup.get_text("\n", strip=True)
    lines = [clean(x) for x in text.splitlines() if clean(x)]

    occurrences = []
    in_section = False

    for line in lines:
        lower = line.lower()

        if "happens on the following dates" in lower:
            in_section = True
            continue

        if in_section:
            if lower in {"admission", "location", "contact", "times", "how to get here"}:
                break

            parsed = parse_occurrence(line)
            if parsed:
                occurrences.append(parsed)

    # Robust fallback: inspect every visible text line for a date/time
    # occurrence. This handles CTvisit pages where the heading and list are
    # wrapped differently.
    if not occurrences:
        for line in lines:
            parsed = parse_occurrence(line)
            if parsed:
                occurrences.append(parsed)

    # De-duplicate while preserving order.
    seen = set()
    result = []
    for start_dt, end_dt in occurrences:
        key = (start_dt, end_dt)
        if key not in seen:
            seen.add(key)
            result.append((start_dt, end_dt))
    return result

def extract_location(soup: BeautifulSoup) -> str:
    text = soup.get_text("\n", strip=True)
    lines = [clean(x) for x in text.splitlines() if clean(x)]

    for i, line in enumerate(lines):
        if line.lower().startswith("location "):
            venue = clean(line[9:])
            following = []
            for nxt in lines[i+1:i+4]:
                low = nxt.lower()
                if low in {"contact", "times", "admission", "how to get here"}:
                    break
                if "map & directions" in low:
                    break
                following.append(nxt)
            parts = [venue] + following
            return ", ".join(x for x in parts if x)

        if line.lower() == "location" and i + 1 < len(lines):
            following = []
            for nxt in lines[i+1:i+5]:
                low = nxt.lower()
                if low in {"contact", "times", "admission", "how to get here"}:
                    break
                if "map & directions" in low:
                    break
                following.append(nxt)
            return ", ".join(following)

    return ""

def scrape_event(page, url: str):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(400)

    soup = BeautifulSoup(page.content(), "html.parser")
    h1 = soup.find("h1")
    title = clean(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        return []

    occurrences = parse_occurrences(soup)
    if not occurrences:
        return []

    location = extract_location(soup)
    description = extract_description(soup)

    result = []
    for start_dt, end_dt in occurrences:
        result.append({
            "title": title,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "location": location,
            "description": description,
            "url": url,
        })
    return result

def build_calendar(items):
    cal = Calendar()
    cal.add("prodid", "-//CTvisit Events Calendar//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Connecticut Events")
    cal.add("x-wr-timezone", "America/New_York")

    seen = set()

    for item in items:
        key = (item["url"], item["start_dt"])
        if key in seen:
            continue
        seen.add(key)

        ev = Event()
        uid_source = f"{item['url']}|{item['start_dt'].isoformat()}"
        ev.add("uid", hashlib.sha256(uid_source.encode()).hexdigest()[:28] + "@ctvisit-calendar")
        ev.add("dtstamp", datetime.utcnow())
        ev.add("summary", item["title"])
        ev.add("dtstart", item["start_dt"])
        ev.add("dtend", item["end_dt"])
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
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1440, "height": 1200},
            user_agent="Mozilla/5.0 (compatible; CTvisitCalendar/1.0)"
        )

        urls = discover_event_urls(page)
        print(f"Found {len(urls)} event URLs")

        all_events = []
        for i, url in enumerate(urls, 1):
            try:
                events = scrape_event(page, url)
                all_events.extend(events)
                if events:
                    print(f"[{i}/{len(urls)}] {events[0]['title']} ({len(events)} occurrence(s))")
                else:
                    print(f"[{i}/{len(urls)}] SKIPPED {url}: no occurrence dates found")
            except Exception as exc:
                print(f"[{i}/{len(urls)}] ERROR {url}: {exc}")

        browser.close()

    all_events.sort(key=lambda x: (x["start_dt"], x["title"]))
    count = build_calendar(all_events)
    print(f"Wrote {OUTPUT} with {count} calendar events")

if __name__ == "__main__":
    main()
