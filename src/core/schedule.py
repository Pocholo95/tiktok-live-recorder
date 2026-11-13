"""
Learns when a creator usually goes live, from their recording history.

Design (agreed with the user, see CLAUDE.md "Live schedules"):
- Output is, per hour of the week (168 bins, UTC), the probability that the
  creator is live at that hour on a given day. The UI shifts it to the
  viewer's local time.
- It counts *frequency*, not presence: one odd midday live among twenty
  night lives leaves midday at ~5 %, far below the "usual" threshold, but
  it is never ignored either.
- Recent days weigh more (half-life HALF_LIFE_DAYS), so a real change of
  habits is picked up within weeks while a one-off fades away.
- Edges are smoothed: a live hour also gives half credit to the hours
  right before and after it.
- Per-weekday probabilities once every weekday has been observed at least
  MIN_WEEKS times, shrunk toward the pooled hour-of-day pattern; before
  that all days are pooled. Only complete days count (not today).
- Nothing is "learned" until MIN_LIVES lives over MIN_DAYS days.

Learned schedules only ever change how *often* a channel is checked (see
`smart_interval`), never whether it is checked.
"""

import math
from datetime import datetime, timedelta, timezone

HALF_LIFE_DAYS = 30
MIN_LIVES = 5
MIN_DAYS = 7
MIN_WEEKS = 3
WEEKDAY_PRIOR_DAYS = 2  # how strongly per-weekday values lean on the overall pattern
USUAL_THRESHOLD = 0.30
# Creators who don't stream every day never reach 30 % at any hour, so an
# hour is also "usual" when it reaches half of the creator's own best hour,
# as long as that is at least MIN_USUAL (noise floor).
RELATIVE_TO_PEAK = 0.5
MIN_USUAL = 0.08
NEIGHBOR_CREDIT = 0.5
FAST_FACTOR = 0.5  # usual hours: half the channel interval...
FAST_MIN_MINUTES = 1  # ...but at least 1 min
SLOW_FACTOR = 2  # other hours: twice the channel interval...
SLOW_MAX_MINUTES = 15  # ...but at most 15 min (never slower than configured)


def parse_utc(value):
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def _live_hours(spans):
    """{(date, hour): credit} for every hour touched by a live (UTC)."""
    credit = {}

    def add(moment, value):
        key = (moment.date(), moment.hour)
        credit[key] = max(credit.get(key, 0.0), value)

    for start, end in spans:
        hour = start.replace(minute=0, second=0, microsecond=0)
        while hour < end:
            add(hour, 1.0)
            add(hour - timedelta(hours=1), NEIGHBOR_CREDIT)
            add(hour + timedelta(hours=1), NEIGHBOR_CREDIT)
            hour += timedelta(hours=1)
    return credit


def _decay(age_days):
    return 0.5 ** (max(age_days, 0) / HALF_LIFE_DAYS)


def compute(spans, observed_since, now=None):
    """
    `spans`: [(start, end)] aware UTC datetimes of the creator's lives.
    `observed_since`: when monitoring began (channel created / first live).

    Returns {"learned", "lives", "days", "mode", "probs"} where `probs` is a
    168-list indexed by weekday*24 + hour (Monday = 0, UTC).
    """
    now = now or datetime.now(timezone.utc)
    spans = [(s, min(e, now)) for s, e in spans if s and e and e > s]
    first = (
        min([observed_since] + [s for s, _ in spans if s]) if spans else observed_since
    )
    first = first or now
    days = max((now - first).total_seconds() / 86400, 0)

    result = {
        "learned": len(spans) >= MIN_LIVES and days >= MIN_DAYS,
        "lives": len(spans),
        "days": int(days),
        "mode": None,
        "probs": [0.0] * 168,
        "threshold": USUAL_THRESHOLD,
    }
    if not spans:
        return result

    # Only *complete* days count: today hasn't happened yet (its night is
    # still ahead), so a live today starts counting tomorrow - otherwise one
    # unusual live today would weigh like a whole pattern.
    day_weight = {}
    day = first.date()
    last_full_day = now.date() - timedelta(days=1)
    while day <= last_full_day:
        day_weight[day] = _decay((now.date() - day).days)
        day += timedelta(days=1)
    if not day_weight:
        return result

    live = _live_hours(spans)
    hits = {}  # (weekday, hour) -> weighted live credit
    for (date, hour), value in live.items():
        if date in day_weight:
            key = (date.weekday(), hour)
            hits[key] = hits.get(key, 0.0) + value * day_weight[date]

    per_weekday = {}
    weekday_counts = {}
    for date, weight in day_weight.items():
        per_weekday[date.weekday()] = per_weekday.get(date.weekday(), 0.0) + weight
        weekday_counts[date.weekday()] = weekday_counts.get(date.weekday(), 0) + 1
    total_weight = sum(day_weight.values())

    by_weekday = len(weekday_counts) == 7 and min(weekday_counts.values()) >= MIN_WEEKS
    result["mode"] = "weekday" if by_weekday else "daily"

    for hour in range(24):
        daily_hits = sum(hits.get((w, hour), 0.0) for w in range(7))
        daily_p = daily_hits / total_weight
        for weekday in range(7):
            p = daily_p
            if by_weekday:
                # Shrink each weekday toward the overall pattern: with only a
                # few Fridays observed, one odd Friday can't make that hour
                # "usual" on its own. The larger of the two is used, so a new
                # every-day habit still shows up quickly.
                weekday_p = (
                    hits.get((weekday, hour), 0.0) + WEEKDAY_PRIOR_DAYS * daily_p
                ) / (per_weekday.get(weekday, 0.0) + WEEKDAY_PRIOR_DAYS)
                p = max(weekday_p, daily_p)
            result["probs"][weekday * 24 + hour] = min(1.0, p)
    result["threshold"] = usual_threshold(result["probs"])
    return result


def usual_threshold(probs):
    """Probability from which an hour counts as usual for this creator."""
    peak = max(probs) if probs else 0.0
    return min(USUAL_THRESHOLD, max(MIN_USUAL, peak * RELATIVE_TO_PEAK))


def probability_at(schedule, moment):
    moment = moment.astimezone(timezone.utc)
    return schedule["probs"][moment.weekday() * 24 + moment.hour]


def is_usual(schedule, moment):
    threshold = schedule.get("threshold", USUAL_THRESHOLD)
    return schedule["learned"] and probability_at(schedule, moment) >= threshold


def smart_interval(base_minutes, schedule, moment):
    """
    (minutes, reason) for a channel with the smart interval on. Before the
    schedule is learned the configured interval is used unchanged.
    """
    if not schedule or not schedule["learned"]:
        return base_minutes, "learning"
    if is_usual(schedule, moment):
        return max(FAST_MIN_MINUTES, math.floor(base_minutes * FAST_FACTOR)), "usual"
    slow = min(base_minutes * SLOW_FACTOR, SLOW_MAX_MINUTES)
    return max(base_minutes, slow), "unusual"
