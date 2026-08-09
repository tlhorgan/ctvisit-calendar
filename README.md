# CTvisit → Proton Calendar

This repository builds an iCalendar (`ctvisit.ics`) feed from Connecticut's official tourism events calendar:

https://ctvisit.com/events

A GitHub Action runs daily and commits the refreshed ICS file.

## Proton Calendar subscription URL

After pushing this repository to GitHub, subscribe to:

https://raw.githubusercontent.com/YOUR-GITHUB-USERNAME/ctvisit-calendar/main/ctvisit.ics

Replace `YOUR-GITHUB-USERNAME` with your GitHub username.

In Proton Calendar, add a calendar from URL and paste the raw GitHub URL.

## Run manually

Actions → Update Connecticut calendar → Run workflow

## Files

- `generate_calendar.py` — discovers CTvisit event pages and converts each listed occurrence into an ICS event.
- `.github/workflows/update-calendar.yml` — runs the generator daily.
- `ctvisit.ics` — generated automatically after the first successful workflow run.

## Recurring events

CTvisit event pages often contain a "Happens on the following Dates" section.
This generator creates a separate calendar occurrence for each listed date/time.
