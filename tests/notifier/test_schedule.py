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


def ids(alerts):
    """`kind:gw:offset` per alert -- a readable identity for assertions.

    Not the state key, which is per game and has its own tests below. Asserting on the
    state keys everywhere would put "fpl"/"draft" into twenty expectations that are not
    about games at all.
    """
    return [f"{a.moment.kind}:{a.moment.gw}:{a.offset_hours}" for a in alerts]


def state_keys(*alerts):
    """The keys a caller would write for these alerts, for seeding `sent`.

    Built from `Alert.keys` rather than spelled out, so a change to the key format
    cannot leave these fixtures quietly asserting against the old one.
    """
    return {key for alert in alerts for key in alert.keys}


def test_the_offsets_are_exactly_one_day_and_two_hours():
    assert OFFSETS_HOURS == (24, 2)


def test_alert_keys_are_one_per_game():
    """Per game, because sources.merge keeps FPL and Draft as separate moments when they
    publish different instants for a gameweek. One key for the pair would let whichever
    fired first suppress the other."""
    assert Alert(GW2_WAIVERS, 24).keys == {"waivers:2:draft:24"}
    assert Alert(GW2_DEADLINE, 2).keys == {"deadline:2:draft:2", "deadline:2:fpl:2"}


def test_a_diverged_deadline_keeps_two_separate_identities():
    """The case the per-game key exists for: the two games hours apart on the same
    gameweek. A shared key would mean the second alert never fires."""
    fpl = Moment("deadline", 2, DEADLINE, frozenset({"fpl"}))
    draft = Moment("deadline", 2, DEADLINE + datetime.timedelta(hours=2), frozenset({"draft"}))
    assert Alert(fpl, 24).keys.isdisjoint(Alert(draft, 24).keys)


def test_a_merged_alert_spends_both_games_keys():
    """A merged moment covers both games, so both keys are spent. Otherwise the pair
    would re-send on every tick on the strength of the unwritten half."""
    to_send, to_retire = due_alerts([GW2_DEADLINE], state_keys(*[
        Alert(GW2_DEADLINE, offset) for offset in OFFSETS_HOURS
    ]), at(hours=-2))
    assert to_send == []
    assert to_retire == []


def test_an_alert_is_still_owed_while_any_of_its_games_is_unsent():
    """Half-spent is not spent. FPL's alert having gone out must not silence Draft's."""
    partial = {"deadline:2:fpl:2", "deadline:2:fpl:24"}
    to_send, _ = due_alerts([GW2_DEADLINE], partial, at(hours=-2))
    assert ids(to_send) == ["deadline:2:2"]


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
    assert ids(to_send) == ["deadline:2:2"]


def test_a_tick_a_second_before_the_trigger_sends_nothing():
    """The 24h alert is seeded because it is genuinely owed by this point -- its trigger
    passed yesterday. Without it this asserts the most-urgent rule, not the boundary."""
    sent = state_keys(Alert(GW2_DEADLINE, 24))
    to_send, _ = due_alerts([GW2_DEADLINE], sent, at(hours=-2, seconds=-1))
    assert to_send == []


def test_the_four_alerts_of_a_gameweek_fall_at_four_distinct_times():
    """The spec's timetable: D-48, D-26, D-24, D-2. Nothing collides on an unmoved
    schedule, so the merge path is defensive rather than routine."""
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
    assert sorted(ids(to_send)) == ["deadline:3:24", "waivers:2:2"]


def test_a_cold_start_two_hours_out_sends_only_the_two_hour_alert():
    """First run with an empty state, two hours before the deadline. Both offsets are
    due -- the 24h trigger passed yesterday and the moment is still ahead -- but
    announcing "in 24 hours" about a deadline two hours away is the one thing this
    service must never do."""
    to_send, to_retire = due_alerts([GW2_DEADLINE], set(), at(hours=-2))
    assert ids(to_send) == ["deadline:2:2"]
    assert ids(to_retire) == ["deadline:2:24"]


def test_a_superseded_alert_is_retired_so_it_cannot_fire_later():
    """Retiring rather than merely skipping. Left unmarked, the 24h alert would still be
    due on the next tick and every tick after it."""
    to_send, to_retire = due_alerts([GW2_DEADLINE], set(), at(hours=-2))
    sent = state_keys(*to_send, *to_retire)
    again_send, again_retire = due_alerts([GW2_DEADLINE], sent, at(hours=-1))
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
    assert sorted(ids(to_send)) == ["deadline:3:2", "waivers:2:2"]


def test_an_already_sent_alert_is_not_resent():
    """Both offsets, because by two hours out both are owed. Seeding only the 2h one
    leaves the 24h one legitimately due and tests nothing about resending."""
    sent = state_keys(Alert(GW2_DEADLINE, 24), Alert(GW2_DEADLINE, 2))
    to_send, to_retire = due_alerts([GW2_DEADLINE], sent, at(hours=-2))
    assert to_send == []
    assert to_retire == []


def test_both_offsets_of_one_moment_are_independent():
    """Sending the 24h alert must not suppress the 2h one."""
    sent = state_keys(Alert(GW2_DEADLINE, 24))
    to_send, _ = due_alerts([GW2_DEADLINE], sent, at(hours=-2))
    assert ids(to_send) == ["deadline:2:2"]


def test_a_late_alert_still_sends_while_its_moment_is_ahead():
    """Down for three hours across the 24h trigger, back up with 21 hours to spare. The
    alert is late but still actionable, so it goes."""
    to_send, to_retire = due_alerts([GW2_DEADLINE], set(), at(hours=-21))
    assert ids(to_send) == ["deadline:2:24"]
    assert to_retire == []


def test_an_alert_whose_moment_has_passed_is_retired_not_sent():
    """Down for a week. Warning about a deadline that has already gone is noise, and a
    long outage would otherwise deliver a burst of it on restart."""
    to_send, to_retire = due_alerts(BOTH, set(), at(hours=1))
    assert to_send == []
    assert sorted(ids(to_retire)) == [
        "deadline:2:2", "deadline:2:24", "waivers:2:2", "waivers:2:24",
    ]


def test_a_retired_alert_is_not_offered_twice():
    """Retirement is only useful if the caller writes the keys; given that, a second
    restart must find nothing left to do -- to send OR to retire."""
    _, to_retire = due_alerts(BOTH, set(), at(hours=1))
    again_send, again_retire = due_alerts(BOTH, state_keys(*to_retire), at(hours=1))
    assert again_send == []
    assert again_retire == []


def test_a_moment_exactly_now_is_retired_rather_than_sent():
    """Zero notice is not a warning."""
    to_send, to_retire = due_alerts([GW2_DEADLINE], set(), DEADLINE)
    assert to_send == []
    # Both, and in trigger order: the moment has passed, so nothing about it is worth
    # sending and everything still outstanding for it is retired.
    assert ids(to_retire) == ["deadline:2:24", "deadline:2:2"]


def test_one_gameweek_passing_does_not_retire_the_next():
    later = Moment("deadline", 3, DEADLINE + datetime.timedelta(days=7), frozenset({"fpl"}))
    to_send, to_retire = due_alerts([GW2_DEADLINE, later], set(), at(hours=1))
    assert to_send == []
    # Spelled out rather than `all(gw == 2)`, which an implementation retiring nothing
    # at all would also satisfy.
    assert ids(to_retire) == ["deadline:2:24", "deadline:2:2"]


def test_results_are_sorted_by_trigger_time():
    moved = Moment("deadline", 3, WAIVERS + datetime.timedelta(hours=2), frozenset({"fpl"}))
    now = WAIVERS - datetime.timedelta(minutes=30)
    to_send, _ = due_alerts([GW2_WAIVERS, moved], set(), now)
    assert len(to_send) == 2  # or the ordering assertion below proves nothing
    assert [a.trigger for a in to_send] == sorted(a.trigger for a in to_send)


def test_a_naive_now_is_rejected():
    """Asserted with no moments, so the guard is the only thing that can raise. With
    moments in hand the naive/aware comparison inside the loop raises TypeError anyway,
    and the test would pass whether or not the guard existed."""
    with pytest.raises(TypeError, match="timezone-aware"):
        due_alerts([], set(), datetime.datetime(2026, 8, 28, 17, 30))


def test_no_moments_is_not_an_error():
    assert due_alerts([], set(), at(hours=-2)) == ([], [])
