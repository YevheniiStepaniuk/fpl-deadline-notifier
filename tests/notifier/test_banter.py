"""banter.py: the owner's 20 lines, the roster parser, and the picker.

Wiring -- whether any of this runs at all, and what happens when it fails -- is
covered in test_main.py instead, the same split test_render.py/test_schedule.py
already have from test_main.py: pure rules here, tick behaviour there.
"""

import itertools
import random

import pytest

from notifier.banter import (
    LINES,
    RosterMember,
    _pick_target,
    eligible_lines,
    next_banter,
    parse_roster,
)

ROSTER = (
    RosterMember("@alice_fpl", "Chelsea"),
    RosterMember("@bob_fpl", "Manchester United"),
    RosterMember("@carol_fpl", "Liverpool"),
    RosterMember("@dave_fpl", "Arsenal"),
)


def _cycle_rand(*indices):
    """A deterministic `rand` that returns the given indices in order, then repeats
    the last one forever -- enough for any test here, since none needs more calls
    than it explicitly seeds."""
    it = itertools.chain(indices, itertools.repeat(indices[-1]))
    return lambda _n: next(it)


# --- parse_roster ---------------------------------------------------------------------


def test_parse_roster_reads_the_owners_real_value():
    raw = (
        "@alice_fpl:Chelsea,@bob_fpl:Manchester United,"
        "@carol_fpl:Liverpool,@dave_fpl:Arsenal"
    )
    assert parse_roster(raw) == ROSTER


def test_parse_roster_trims_whitespace_around_each_field():
    assert parse_roster(" @a : Team One , @b:Team Two ") == (
        RosterMember("@a", "Team One"),
        RosterMember("@b", "Team Two"),
    )


def test_parse_roster_splits_on_the_first_colon_only():
    """A team name containing a colon of its own must survive whole in `team`, not
    get truncated at the first one."""
    assert parse_roster("@a:Team: Reserves") == (RosterMember("@a", "Team: Reserves"),)


@pytest.mark.parametrize(
    "raw",
    [
        "@a-no-colon",  # no separator at all
        "@a:",  # empty team
        ":Team",  # empty handle
        "",  # empty entry between two commas
    ],
)
def test_parse_roster_skips_one_malformed_entry_without_refusing_the_rest(raw):
    """The spec's tolerance requirement: a bad entry is dropped, not fatal."""
    assert parse_roster(f"@good:Team,{raw},@also_good:Other") == (
        RosterMember("@good", "Team"),
        RosterMember("@also_good", "Other"),
    )


def test_parse_roster_of_empty_string_is_the_empty_tuple():
    assert parse_roster("") == ()


def test_parse_roster_of_only_whitespace_is_the_empty_tuple():
    assert parse_roster("   ") == ()


# --- eligible_lines ---------------------------------------------------------------------


def test_no_roster_leaves_only_the_six_generic_lines_eligible():
    lines = eligible_lines(())
    assert len(lines) == 6
    assert all(not line.needs_name for line in lines)


def test_a_roster_makes_every_line_eligible():
    assert len(eligible_lines(ROSTER)) == len(LINES) == 20


# --- next_banter: pairing ---------------------------------------------------------------


def test_name_and_team_always_come_from_the_same_roster_entry():
    """The spec's explicit pin: {name} and {team} are a pair, not two independent
    picks. Line 13 is "{name} trusting {team} again. Bold." -- forcing that line and
    the second roster member (index 1) proves the rendered team is that member's
    own, never another's."""
    line_index = [line.id for line in LINES].index("13")
    text, _used, target = next_banter(
        frozenset(), None, ROSTER, _cycle_rand(line_index, 1)
    )
    assert text == "@bob_fpl trusting Manchester United again. Bold."
    assert target == "@bob_fpl"


@pytest.mark.parametrize("line_id", [line.id for line in LINES if line.needs_team])
def test_every_name_and_team_line_pairs_correctly_for_every_roster_member(line_id):
    """The general case behind the test above, across all 8 name+team lines and all
    4 roster members: whichever member is picked, the {team} in the rendered text
    must be exactly that member's own team."""
    line_index = [line.id for line in LINES].index(line_id)
    for member_index, member in enumerate(ROSTER):
        text, _used, target = next_banter(
            frozenset(), None, ROSTER, _cycle_rand(line_index, member_index)
        )
        assert target == member.handle
        assert member.handle in text
        assert member.team in text
        # No other roster member's team leaked in.
        assert not any(other.team in text for other in ROSTER if other is not member)


# --- next_banter: cycling ----------------------------------------------------------------


def test_no_line_repeats_within_one_cycle():
    """Draw every eligible line once, always taking index 0 of whatever remains --
    that only ever produces 20 distinct ids without ever re-selecting one already
    drawn if (and only if) `next_banter` actually removes used ids from the pool
    each time, which is exactly the rule under test."""
    used = frozenset()
    for _ in range(len(LINES)):
        before = used
        _text, used, _target = next_banter(used, None, ROSTER, lambda n: 0)
        assert used - before  # this draw added exactly one new, previously-unused id
    assert used == frozenset(line.id for line in LINES)


def test_the_cycle_resets_once_every_eligible_line_has_been_used():
    """One past the full 20-line cycle: the 21st pick must come from a freshly
    reset pool (used shrinks back down) rather than finding nothing left."""
    used = frozenset(line.id for line in LINES)  # a just-completed cycle
    text, new_used, _target = next_banter(used, None, ROSTER, lambda n: 0)
    assert text  # a line was rendered, not an IndexError
    assert len(new_used) == 1  # the pool reset and exactly one new pick was recorded


def test_a_completed_cycle_with_no_roster_resets_to_the_six_generic_lines():
    used = frozenset(line.id for line in LINES if not line.needs_name)  # all 6 used
    text, new_used, target = next_banter(used, None, (), lambda n: 0)
    assert text
    assert target is None
    assert len(new_used) == 1


# --- next_banter: consecutive target -------------------------------------------------


def test_the_same_member_is_not_targeted_on_two_consecutive_picks():
    """`_pick_target` is the one piece of `next_banter` responsible for the
    consecutive-target rule -- tested directly here (this codebase already imports
    other single-underscore helpers straight from `__main__.py` in test_main.py, so
    this is not a new pattern) with a real, seeded random source across enough
    picks that repetition would show up if the exclusion were not actually
    happening."""
    rand = random.Random(1).randrange
    last_target = None
    targets = []
    for _ in range(50):
        member = _pick_target(ROSTER, last_target, rand)
        last_target = member.handle
        targets.append(last_target)
    for earlier, later in zip(targets, targets[1:]):
        assert earlier != later


def test_a_no_target_line_does_not_block_the_next_pick_from_any_member():
    """A generic line (no target) resets `last_target` to None -- the member
    excluded by the *previous* targeted pick must be pickable again immediately
    after a gap, since nothing about them was actually repeated back-to-back."""
    generic_index = [line.id for line in LINES].index("1")
    line13_index = [line.id for line in LINES].index("13")
    used = frozenset()
    # Pick 1: targets member 0.
    _text, used, last_target = next_banter(
        used, None, ROSTER, _cycle_rand(line13_index, 0)
    )
    assert last_target == ROSTER[0].handle
    # Pick 2: a generic line, no target.
    _text, used, last_target = next_banter(used, last_target, ROSTER, _cycle_rand(generic_index))
    assert last_target is None
    # Pick 3: member 0 again is allowed, since pick 2 targeted nobody.
    text, _used, last_target = next_banter(
        used, last_target, ROSTER, _cycle_rand(line13_index, 0)
    )
    assert last_target == ROSTER[0].handle
    assert ROSTER[0].handle in text


# --- next_banter: degrade cases -------------------------------------------------------


def test_an_empty_roster_only_ever_renders_one_of_the_six_generic_lines():
    used = frozenset()
    for _ in range(12):  # two full cycles
        text, used, target = next_banter(used, None, (), lambda n: 0)
        assert target is None
        assert text in {line.text for line in LINES if not line.needs_name}


def test_a_roster_of_one_does_not_deadlock_or_raise():
    """The spec's explicit callout: with only one member, the consecutive-target
    rule cannot be satisfied once a targeted line follows another targeted line --
    the sensible compromise (see banter._pick_target) is to repeat that member
    rather than crash or hang."""
    solo = (RosterMember("@only_one", "Solo FC"),)
    line13_index = [line.id for line in LINES].index("13")
    used = frozenset()
    last_target = None
    for _ in range(5):
        text, used, last_target = next_banter(
            used, last_target, solo, _cycle_rand(line13_index, 0)
        )
        assert last_target == "@only_one"
        assert "@only_one" in text
        assert "Solo FC" in text


# The pure `load_banter_state` loader (and the full State/save_state round trip) are
# tested in test_state.py, alongside every other State field's loader -- the same
# home decode_pending_greeting's tests have, for the same reason.
