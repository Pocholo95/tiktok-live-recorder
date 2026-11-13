import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import schedule  # noqa: E402

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)  # a Friday


def _nightly(days, start_hour=22, hours=3, until=NOW):
    """One live per night for the last `days` days."""
    spans = []
    for d in range(1, days + 1):
        day = (until - timedelta(days=d)).replace(hour=start_hour, minute=5)
        spans.append((day, day + timedelta(hours=hours)))
    return spans


def test_not_learned_until_enough_lives_and_days():
    spans = _nightly(4)
    s = schedule.compute(spans, NOW - timedelta(days=4), now=NOW)
    assert not s["learned"] and s["lives"] == 4

    s = schedule.compute(_nightly(5), NOW - timedelta(days=5), now=NOW)
    assert not s["learned"]  # 5 lives but only 5 days

    s = schedule.compute(_nightly(8), NOW - timedelta(days=8), now=NOW)
    assert s["learned"]


def test_one_midday_live_does_not_change_a_night_schedule():
    spans = _nightly(20)
    midday = NOW - timedelta(hours=1)  # today 11:00 - the "hoy a medio día" case
    spans.append((midday, midday + timedelta(minutes=50)))
    s = schedule.compute(spans, NOW - timedelta(days=20), now=NOW)

    night = NOW.replace(hour=23)
    noon = NOW.replace(hour=11)
    assert s["learned"]
    assert schedule.probability_at(s, night) > 0.8
    assert schedule.probability_at(s, noon) < schedule.USUAL_THRESHOLD
    assert schedule.is_usual(s, night) and not schedule.is_usual(s, noon)

    # Smart interval with a 5 min channel: faster at night, slower at noon,
    # but noon is still checked (never disabled).
    assert schedule.smart_interval(5, s, night) == (2, "usual")
    assert schedule.smart_interval(5, s, noon) == (10, "unusual")


def test_slow_interval_is_capped_and_never_below_configured():
    s = schedule.compute(_nightly(20), NOW - timedelta(days=20), now=NOW)
    noon = NOW.replace(hour=11)
    assert schedule.smart_interval(10, s, noon) == (15, "unusual")  # 20 -> cap 15
    assert schedule.smart_interval(30, s, noon) == (30, "unusual")  # never faster
    assert schedule.smart_interval(1, s, NOW.replace(hour=23)) == (1, "usual")


def test_learning_keeps_configured_interval():
    s = schedule.compute(_nightly(2), NOW - timedelta(days=2), now=NOW)
    assert schedule.smart_interval(5, s, NOW) == (5, "learning")


def test_recent_habits_win_over_old_ones():
    old_nights = _nightly(30, until=NOW - timedelta(days=60))
    recent_mornings = _nightly(20, start_hour=9, hours=2)
    s = schedule.compute(
        old_nights + recent_mornings, NOW - timedelta(days=90), now=NOW
    )
    assert schedule.is_usual(s, NOW.replace(hour=9))
    assert not schedule.is_usual(s, NOW.replace(hour=23))


def test_neighbor_hours_get_partial_credit():
    s = schedule.compute(
        _nightly(10, start_hour=22, hours=1), NOW - timedelta(days=10), now=NOW
    )
    p22 = schedule.probability_at(s, NOW.replace(hour=22))
    p21 = schedule.probability_at(s, NOW.replace(hour=21))
    p18 = schedule.probability_at(s, NOW.replace(hour=18))
    assert p22 > p21 > 0 and p18 == 0
    assert abs(p21 - p22 * schedule.NEIGHBOR_CREDIT) < 0.05


def test_weekday_mode_after_three_weeks():
    fridays = []
    day = NOW - timedelta(days=7)  # last Friday
    for _ in range(5):
        start = day.replace(hour=20, minute=0)
        fridays.append((start, start + timedelta(hours=2)))
        day -= timedelta(days=7)
    s = schedule.compute(fridays, NOW - timedelta(days=35), now=NOW)

    assert s["mode"] == "weekday"
    friday_night = (NOW - timedelta(days=7)).replace(hour=20)
    monday_night = (NOW - timedelta(days=4)).replace(hour=20)
    assert schedule.is_usual(s, friday_night)
    assert not schedule.is_usual(s, monday_night)


def test_daily_mode_before_three_weeks():
    s = schedule.compute(_nightly(10), NOW - timedelta(days=10), now=NOW)
    assert s["mode"] == "daily"
    # Pooled: every weekday has the same hour profile.
    assert len({tuple(s["probs"][d * 24 : d * 24 + 24]) for d in range(7)}) == 1


def test_midday_live_still_not_usual_once_it_is_history():
    spans = _nightly(20)
    midday = NOW - timedelta(hours=1)
    spans.append((midday, midday + timedelta(minutes=50)))
    tomorrow = NOW + timedelta(days=1)
    s = schedule.compute(spans, NOW - timedelta(days=20), now=tomorrow)

    next_friday_noon = (NOW + timedelta(days=7)).replace(hour=11)
    assert not schedule.is_usual(s, next_friday_noon)
    assert schedule.is_usual(s, tomorrow.replace(hour=23))


def test_lives_today_only_count_from_tomorrow():
    start = NOW.replace(hour=9)
    s = schedule.compute(
        [(start, start + timedelta(hours=1))], NOW - timedelta(days=3), now=NOW
    )
    assert s["probs"] == [0.0] * 168


def test_part_time_creator_still_gets_usual_hours():
    # Like @miguelrv777: evenings, but only ~1 day in 4 -> peak far below 30 %.
    spans = []
    for d in (2, 6, 10, 14, 18):
        start = (NOW - timedelta(days=d)).replace(hour=20, minute=10)
        spans.append((start, start + timedelta(hours=2)))
    s = schedule.compute(spans, NOW - timedelta(days=20), now=NOW)

    assert s["learned"] and max(s["probs"]) < schedule.USUAL_THRESHOLD
    assert s["threshold"] < schedule.USUAL_THRESHOLD
    assert schedule.is_usual(s, NOW.replace(hour=21))
    assert not schedule.is_usual(s, NOW.replace(hour=11))
    assert schedule.smart_interval(4, s, NOW.replace(hour=21)) == (2, "usual")


def test_relative_threshold_has_a_noise_floor():
    assert schedule.usual_threshold([0.04] * 168) == schedule.MIN_USUAL
    assert schedule.usual_threshold([0.0] * 167 + [1.0]) == schedule.USUAL_THRESHOLD
    assert schedule.usual_threshold([0.0] * 167 + [0.2]) == 0.1


def test_creator_without_channel_uses_first_live():
    # Deleted channel: no "observed since" date, recordings still exist.
    s = schedule.compute(_nightly(10), None, now=NOW)
    assert s["lives"] == 10 and s["days"] >= 9
    assert schedule.compute([], None, now=NOW)["lives"] == 0
