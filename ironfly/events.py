"""Macro event calendar: FOMC decisions + the three scheduled data prints that move SPX vol
(CPI, the jobs report, and core PCE), plus computed monthly opex / quad witching.

Exact 2026 dates below are sourced from the official calendars: federalreserve.gov/monetarypolicy
/fomccalendars.htm (FOMC), bls.gov/schedule (CPI, Employment Situation), bea.gov/news/schedule
(Personal Income and Outlays, which contains the PCE price index) — pulled via web search on
2026-09-13. Re-verify each year; the Fed and BLS both publish 12-18 months ahead. For any date
outside SEED's coverage, NFP falls back to the first-Friday heuristic (mostly right, sometimes off
by a week around holidays) and CPI/PCE emit nothing rather than a bad guess. Maintain
data/events.json for anything not seeded here (extra CPI/PCE prints, earnings, etc.)."""
from __future__ import annotations
import calendar
import json
from datetime import date, timedelta
from pathlib import Path

FOMC_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
             "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]

CPI_2026 = ["2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10",
            "2026-07-14", "2026-08-12", "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10"]

NFP_2026 = ["2026-01-09", "2026-02-11", "2026-03-06", "2026-04-03", "2026-05-08", "2026-06-05",
            "2026-07-02", "2026-08-07", "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04"]

# BEA's "Personal Income and Outlays" release, which carries the PCE price index. BEA sometimes
# runs two releases close together to catch a delayed prior month; every date below is real.
PCE_2026 = ["2026-01-22", "2026-02-20", "2026-03-13", "2026-04-09", "2026-04-30", "2026-05-28",
            "2026-06-25", "2026-07-30", "2026-08-26", "2026-09-30", "2026-10-29", "2026-11-25",
            "2026-12-23"]

SEED = ([{"date": d, "name": "FOMC"} for d in FOMC_2026]
        + [{"date": d, "name": "CPI"} for d in CPI_2026]
        + [{"date": d, "name": "NFP"} for d in NFP_2026]
        + [{"date": d, "name": "PCE"} for d in PCE_2026])

_SEEDED_MONTHS = {(date.fromisoformat(d).year, date.fromisoformat(d).month) for d in NFP_2026}


def _nth_friday(y: int, m: int, n: int) -> date:
    c = calendar.Calendar()
    fridays = [d for d in c.itermonthdates(y, m) if d.month == m and d.weekday() == 4]
    return fridays[n - 1]


def load(path: str | None) -> list[dict]:
    ev = list(SEED)
    p = Path(path) if path else None
    if p and p.exists():
        ev += json.loads(p.read_text())
    return ev


def events_between(start: date, end: date, extra: list[dict] | None = None) -> list[dict]:
    """All events with start <= date <= end. Adds computed opex/quad-witching, and an NFP
    first-Friday fallback for any month not covered by NFP_2026 above."""
    out = []
    for e in (extra or []):
        d = date.fromisoformat(e["date"])
        if start <= d <= end:
            out.append({"date": d, "name": e["name"]})
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        d3 = _nth_friday(y, m, 3)
        if start <= d3 <= end:
            out.append({"date": d3, "name": "QUAD_WITCHING" if m in (3, 6, 9, 12) else "OPEX"})
        if (y, m) not in _SEEDED_MONTHS:
            d1 = _nth_friday(y, m, 1)
            if start <= d1 <= end:
                out.append({"date": d1, "name": "NFP(approx)"})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(out, key=lambda e: e["date"])


def blocking_events(today: date, days_ahead: int, extra: list[dict]) -> list[dict]:
    """Events that should block a *new entry*: high-impact only (not opex)."""
    ev = events_between(today, today + timedelta(days=days_ahead), extra)
    return [e for e in ev if e["name"] not in ("OPEX",)]
