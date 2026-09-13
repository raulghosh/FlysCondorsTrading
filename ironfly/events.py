"""Macro event calendar. JSON file of {"date": "YYYY-MM-DD", "name": "FOMC"} entries plus
computed monthly opex / quad witching / NFP (first Friday, approximate)."""
from __future__ import annotations
import calendar
import json
from datetime import date, timedelta
from pathlib import Path

# FOMC decision days published for 2026. Maintain data/events.json for CPI/NFP/PCE etc.
SEED = [
    {"date": "2026-01-28", "name": "FOMC"}, {"date": "2026-03-18", "name": "FOMC"},
    {"date": "2026-04-29", "name": "FOMC"}, {"date": "2026-06-17", "name": "FOMC"},
    {"date": "2026-07-29", "name": "FOMC"}, {"date": "2026-09-16", "name": "FOMC"},
    {"date": "2026-10-28", "name": "FOMC"}, {"date": "2026-12-09", "name": "FOMC"},
]


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
    """All events with start <= date <= end. Includes computed opex/quad-witching/NFP(approx)."""
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
        d1 = _nth_friday(y, m, 1)
        if start <= d1 <= end:
            out.append({"date": d1, "name": "NFP(approx)"})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(out, key=lambda e: e["date"])


def blocking_events(today: date, days_ahead: int, extra: list[dict]) -> list[dict]:
    """Events that should block a *new entry*: high-impact only (not opex)."""
    ev = events_between(today, today + timedelta(days=days_ahead), extra)
    return [e for e in ev if e["name"] not in ("OPEX",)]
