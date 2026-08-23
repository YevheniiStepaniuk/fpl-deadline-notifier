import datetime

import pytest

from notifier.schedule import OFFSETS_HOURS, Alert, due_alerts
from notifier.sources import Moment

DEADLINE = datetime.datetime(2026, 8, 28, 17, 30, tzinfo=datetime.UTC)
WAIVERS = datetime.datetime(2026, 8, 27, 17, 30, tzinfo=datetime.UTC)

GW2_DEADLINE = Moment("deadline", 2, DEADLINE, frozenset({"fpl", "draft"}))
GW2_WAIVERS = Moment("waivers", 2, WAIVERS, frozenset({"draft"}))
BOTH = [GW2_DEADLINE, GW2_WAIVERS]


def at(**delta):
    """A `now` expressed relative to the GW2 deadline, e.g. at(hours=-2)."""
    return DEADLINE + datetime.timedelta(**delta)


def keys(alerts):
    return [a.key for a in alerts]


def test_the_offsets_are_exactly_one_day_and_two_hours():
    assert OFFSETS_HOURS == (24, 2)


def test_alert_key_format():
    assert Alert(GW2_WAIVERS, 24).key == "waivers:2:24"
    assert Alert(GW2_DEADLINE, 2).key == "deadline:2:2"


def test_alert_trigger_is_the_offset_before_the_moment():
    assert Alert(GW2_DEADLINE, 2).trigger == at(hours=-2)


def test_nothing_is_due_long_before_the_first_trigger():
    to_send, to_retire = due_alerts(BOTH, set(), at(days=-7))
    assert to_send == []
    assert to_retire == []


def test_the_two_hour_alert_fires_exactly_on_its_trigger():
    """A tick landing precisely on the boundary must send, not wait for the next one.
    With a 60s poll a strict `>` would be right 59 times out of 60 and look correct in
    every hand test."""
    to_send, _ = due_alerts([GW2_DEADLINE], set(), at(hours=-2))
    assert keys(to_send) == ["deadline:2:2"]


def test_a_tick_a_second_before_the_trigger_sends_nothing():
    """The 24h key is seeded because it is genuinely owed by this point -- its trigger
    passed yesterday. Without it this asserts the most-urgent rule, not the boundary."""
    to_send, _ = due_alerts([GW2_DEADLINE], {"deadline:2:24"}, at(hours=-2, seconds=-1))
    assert to_send == []


def test_the_four_alerts_of_a_gameweek_fall_at_four_distinct_times():
    """The spec's timetable: D-48, D-26, D-24, D-2. Nothing collides on an unmoved
    schedule, so the merge path below is defensive rather than routine."""
    triggers = {Alert(m, o).trigger for m in BOTH for o in OFFSETS_HOURS}
    assert len(triggers) == 4
    assert sorted(triggers) == [at(hours=-48), at(hours=-26), at(hours=-24), at(hours=-2)]


def test_the_deadline_day_alert_fires_when_the_waiver_window_shuts():
    """Draft's waivers_before_deadline_hours is 24, so these coincide by construction.
    One is a warning about tomorrow, the other is about right now, and both are wanted."""
    assert Alert(GW2_DEADLINE, 24).trigger == GW2_WAIVERS.when


def test_a_moved_deadline_can_put_two_alerts_in_one_tick():
    """FPL reschedules deadlines. This is the case the list-taking renderer exists for.

    Two *different* moments, so the most-urgent rule does not collapse them: it works
    per moment, not per tick.
    """
    moved = Moment("deadline", 3, WAIVERS + datetime.timedelta(hours=2), frozenset({"fpl"}))
    now = WAIVERS - datetime.timedelta(minutes=30)
    to_send, _ = due_alerts([GW2_WAIVERS, moved], set(), now)
    assert sorted(keys(to_send)) == ["deadline:3:24", "waivers:2:2"]


def test_an_already_sent_key_is_not_resent():
    """Both keys, because by two hours out both offsets are owed. Seeding only the 2h
    one leaves the 24h one legitimately due and tests nothing about resending."""
    sent = {"deadline:2:24", "deadline:2:2"}
    to_send, to_retire = due_alerts([GW2_DEADLINE], sent, at(hours=-2))
    assert to_send == []
    assert to_retire == []


def test_both_offsets_of_one_moment_are_independent():
    """Sending the 24h alert must not suppress the 2h one."""
    to_send, _ = due_alerts([GW2_DEADLINE], {"deadline:2:24"}, at(hours=-2))
    assert keys(to_send) == ["deadline:2:2"]


def test_a_cold_start_two_hours_out_sends_only_the_two_hour_alert():
    """First run with an empty state, two hours before the deadline. Both offsets are
    due -- the 24h trigger passed yesterday and the moment is still ahead -- but
    announcing "in 24 hours" about a deadline two hours away is the one thing this
    service must never do."""
    to_send, to_retire = due_alerts([GW2_DEADLINE], set(), at(hours=-2))
    assert keys(to_send) == ["deadline:2:2"]
    assert keys(to_retire) == ["deadline:2:24"]


def test_a_superseded_alert_is_retired_so_it_cannot_fire_later():
    """Retiring rather than merely skipping. Left unmarked, the 24h alert would still
    be due on the next tick and every tick after it."""
    _, to_retire = due_alerts([GW2_DEADLINE], set(), at(hours=-2))
    again_send, again_retire = due_alerts(
        [GW2_DEADLINE], {a.key for a in to_retire} | {"deadline:2:2"}, at(hours=-1)
    )
    assert again_send == []
    assert again_retire == []


def test_the_rule_applies_per_moment_not_per_tick():
    """Two moments each with both offsets due. One alert survives from each, not one
    overall -- a waiver window and a deadline are different things to be late for."""
    now = WAIVERS - datetime.timedelta(minutes=30)
    other = Moment("deadline", 3, WAIVERS + datetime.timedelta(hours=1), frozenset({"fpl"}))
    to_send, _ = due_alerts([GW2_WAIVERS, other], set(), now)
    # Both offsets are due for both moments here -- `other` sits an hour past the waiver
    # moment, so its 2h trigger has also passed -- so each contributes its 2h alert.
    assert sorted(keys(to_send)) == ["deadline:3:2", "waivers:2:2"]


def test_a_late_alert_still_sends_while_its_moment_is_ahead():
    """Down for three hours across the 24h trigger, back up with 21 hours to spare. The
    alert is late but still actionable, so it goes."""
    to_send, to_retire = due_alerts([GW2_DEADLINE], set(), at(hours=-21))
    assert keys(to_send) == ["deadline:2:24"]
    assert to_retire == []


def test_an_alert_whose_moment_has_passed_is_retired_not_sent():
    """Down for a week. Warning about a deadline that has already gone is noise, and a
    long outage would otherwise deliver a burst of it on restart."""
    to_send, to_retire = due_alerts(BOTH, set(), at(hours=1))
    assert to_send == []
    assert sorted(keys(to_retire)) == [
        "deadline:2:2", "deadline:2:24", "waivers:2:2", "waivers:2:24",
    ]


def test_a_retired_alert_is_not_offered_twice():
    """Retirement is only useful if the caller writes the keys; given that, a second
    restart must find nothing left to do."""
    _, to_retire = due_alerts(BOTH, set(), at(hours=1))
    _, again = due_alerts(BOTH, {a.key for a in to_retire}, at(hours=1))
    assert again == []


def test_a_moment_exactly_now_is_retired_rather_than_sent():
    """Zero notice is not a warning."""
    to_send, to_retire = due_alerts([GW2_DEADLINE], set(), DEADLINE)
    assert to_send == []
    # Both, and in trigger order: the moment has passed, so nothing about it is worth
    # sending and everything still outstanding for it is retired.
    assert keys(to_retire) == ["deadline:2:24", "deadline:2:2"]


def test_one_gameweek_passing_does_not_retire_the_next():
    later = Moment("deadline", 3, DEADLINE + datetime.timedelta(days=7), frozenset({"fpl"}))
    to_send, to_retire = due_alerts([GW2_DEADLINE, later], set(), at(hours=1))
    assert keys(to_send) == []
    assert all(a.moment.gw == 2 for a in to_retire)


def test_results_are_sorted_by_trigger_time():
    moved = Moment("deadline", 3, WAIVERS + datetime.timedelta(hours=2), frozenset({"fpl"}))
    now = WAIVERS - datetime.timedelta(minutes=30)
    to_send, _ = due_alerts([GW2_WAIVERS, moved], set(), now)
    assert len(to_send) == 2  # or the ordering assertion below proves nothing
    assert [a.trigger for a in to_send] == sorted(a.trigger for a in to_send)


def test_a_naive_now_is_rejected():
    """Comparing a naive datetime against an aware one raises TypeError deep inside the
    comparison. Failing here names the actual problem."""
    with pytest.raises(TypeError):
        due_alerts(BOTH, set(), datetime.datetime(2026, 8, 28, 17, 30))


def test_no_moments_is_not_an_error():
    assert due_alerts([], set(), at(hours=-2)) == ([], [])
