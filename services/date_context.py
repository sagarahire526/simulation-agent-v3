"""
Date context helper.

LLMs in this system schedule work and forecast completion dates from a single
"today" anchor. Reasoning models reliably mis-compute weekday arithmetic from
a bare ISO date — they round to whole weeks, or assume every plan starts
Monday — so we pre-compute the facts they need (day-of-week, workdays
remaining this week, next Monday, ISO week) and inject the result wherever a
prompt currently uses `{today_date}`.

The placeholder name and call sites stay unchanged; only the *value* gets
richer.
"""
from __future__ import annotations

from datetime import date, timedelta


_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def today_date_context(today: date | None = None) -> str:
    """
    Multi-line date context block for prompt injection.

    The optional `today=` argument exists for verification and unit-testing —
    in production, callers omit it and the helper uses `date.today()`.
    """
    today = today or date.today()
    weekday = today.weekday()  # Mon=0 … Sun=6
    workdays_left = max(0, 5 - weekday) if weekday < 5 else 0
    days_to_next_monday = (7 - weekday) % 7 or 7
    next_monday = today + timedelta(days=days_to_next_monday)
    iso_year, iso_week, _ = today.isocalendar()
    remaining_label = (
        f"{workdays_left} ({', '.join(_DAY_NAMES[weekday:5])})"
        if weekday < 5
        else "0 — weekend"
    )
    return (
        f"{today.isoformat()} ({today.strftime('%A')})\n"
        f"- Workdays remaining this week (incl. today): {remaining_label}\n"
        f"- Next Monday: {next_monday.isoformat()}\n"
        f"- Current ISO week: {iso_year}-W{iso_week:02d}\n"
        "- Scheduling rule: Week 1 starts TODAY and contains only the "
        "workdays-remaining count above; subsequent weeks start each Monday."
    )
