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
    BanterLine,
    RosterMember,
    _pick_target,
    eligible_lines,
    next_banter,
    parse_roster,
)

ROSTER = (
    RosterMember("@alice_fpl", "", "Chelsea"),
    RosterMember("@bob_fpl", "", "Manchester United"),
    RosterMember("@carol_fpl", "", "Liverpool"),
    RosterMember("@dave_fpl", "", "Arsenal"),
)


def _cycle_rand(*indices):
    """A deterministic `rand` that returns the given indices in order, then repeats
    the last one forever -- enough for any test here, since none needs more calls
    than it explicitly seeds."""
    it = itertools.chain(indices, itertools.repeat(indices[-1]))
    return lambda _n: next(it)


# --- parse_roster ---------------------------------------------------------------------


def test_parse_roster_reads_the_owners_real_value():
    """The exact three-field value the owner will actually set NOTIFIER_ROSTER to."""
    raw = (
        "@thecouriersix:Yevhenii Stepaniuk:Chelsea,"
        "@romanusyk:Roman Usyk:Manchester United,"
        "@just_yuricle:Yurii Krat:Liverpool,"
        "@d_vodotiiets:Denys Vodotyets:Arsenal"
    )
    assert parse_roster(raw) == (
        RosterMember("@thecouriersix", "Yevhenii Stepaniuk", "Chelsea"),
        RosterMember("@romanusyk", "Roman Usyk", "Manchester United"),
        RosterMember("@just_yuricle", "Yurii Krat", "Liverpool"),
        RosterMember("@d_vodotiiets", "Denys Vodotyets", "Arsenal"),
    )


def test_parse_roster_three_field_entry_parses_all_three_values():
    assert parse_roster("@romanusyk:Roman Usyk:Manchester United") == (
        RosterMember("@romanusyk", "Roman Usyk", "Manchester United"),
    )


def test_parse_roster_two_field_entry_still_parses_with_empty_fpl_name():
    """Not politeness -- a container in production runs today with this exact
    two-field form in its .env, and must keep working unchanged."""
    assert parse_roster("@bob_fpl:Manchester United") == (
        RosterMember("@bob_fpl", "", "Manchester United"),
    )


def test_parse_roster_mixes_two_field_and_three_field_entries_in_one_roster():
    assert parse_roster("@bob_fpl:Manchester United,@romanusyk:Roman Usyk:Man Utd") == (
        RosterMember("@bob_fpl", "", "Manchester United"),
        RosterMember("@romanusyk", "Roman Usyk", "Man Utd"),
    )


def test_parse_roster_trims_whitespace_around_each_field():
    assert parse_roster(" @a : Roman Usyk : Team One , @b:Team Two ") == (
        RosterMember("@a", "Roman Usyk", "Team One"),
        RosterMember("@b", "", "Team Two"),
    )


def test_parse_roster_three_field_team_containing_a_colon_survives():
    """A team name containing a colon of its own must survive whole in `team`, not
    get truncated -- splitting stops after the first two colons (handle, fpl_name),
    so anything past that, colons included, stays part of `team`."""
    assert parse_roster("@a:Roman Usyk:Team: Reserves") == (
        RosterMember("@a", "Roman Usyk", "Team: Reserves"),
    )


@pytest.mark.parametrize(
    "raw",
    [
        "@a-no-colon",  # no separator at all
        "@a:",  # empty team, two-field form
        ":Team",  # empty handle, two-field form
        "@a::",  # empty fpl_name and empty team, three-field form
        "",  # empty entry between two commas
    ],
)
def test_parse_roster_skips_one_malformed_entry_without_refusing_the_rest(raw):
    """The spec's tolerance requirement: a bad entry is dropped, not fatal."""
    assert parse_roster(f"@good:Team,{raw},@also_good:Other") == (
        RosterMember("@good", "", "Team"),
        RosterMember("@also_good", "", "Other"),
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
    solo = (RosterMember("@only_one", "", "Solo FC"),)
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


# --- fpl_name: {name} unchanged, {fpl_name} wired through -----------------------------


def test_name_still_renders_as_the_telegram_handle_not_the_fpl_name():
    """Spec pin: {name} must keep rendering the handle, unchanged, now that a
    member also carries an fpl_name -- these messages are live."""
    roster = (RosterMember("@romanusyk", "Roman Usyk", "Manchester United"),)
    line13_index = [line.id for line in LINES].index("13")
    text, _used, target = next_banter(
        frozenset(), None, roster, _cycle_rand(line13_index, 0)
    )
    assert text == "@romanusyk trusting Manchester United again. Bold."
    assert "Roman Usyk" not in text
    assert target == "@romanusyk"


def test_a_line_declaring_fpl_name_renders_it(monkeypatch):
    """{fpl_name} is wired through the same substitution path as {name}/{team} in
    next_banter, even though none of the owner's 20 lines use it yet -- proven here
    with a line built just for the test, via monkeypatching LINES the same way
    test_config.py reloads a module to isolate a test from real module state."""
    fpl_line = BanterLine("fpl-test", "{name} plays as {fpl_name}.", True, False, True)
    monkeypatch.setattr("notifier.banter.LINES", (fpl_line,))
    roster = (RosterMember("@romanusyk", "Roman Usyk", "Manchester United"),)
    text, used, target = next_banter(frozenset(), None, roster, _cycle_rand(0, 0))
    assert text == "@romanusyk plays as Roman Usyk."
    assert used == frozenset({"fpl-test"})
    assert target == "@romanusyk"


def test_a_fpl_name_line_is_ineligible_when_nobody_in_the_roster_has_one(monkeypatch):
    """Same degrade rule `eligible_lines` already applies when no roster exists at
    all for a {name} line: a {fpl_name} line with nothing to fill it drops out of
    the pool rather than rendering blank."""
    generic = BanterLine("generic-test", "no target needed here", False, False)
    fpl_line = BanterLine("fpl-test", "{fpl_name}", True, False, True)
    monkeypatch.setattr("notifier.banter.LINES", (generic, fpl_line))
    roster_with_no_fpl_names = (RosterMember("@bob_fpl", "", "Man Utd"),)
    assert eligible_lines(roster_with_no_fpl_names) == (generic,)


def test_a_fpl_name_line_never_targets_a_member_whose_fpl_name_is_empty():
    """The spec's explicit callout: in a *mixed* roster, a {fpl_name} line stays
    eligible (someone can fill it) but must skip straight past any member whose own
    fpl_name is blank when picking who to target, rather than rendering blank for
    them."""
    mixed = (
        RosterMember("@no_name", "", "Everton"),
        RosterMember("@has_name", "Roman Usyk", "Man City"),
    )
    rand = random.Random(2).randrange
    last_target = None
    for _ in range(20):
        member = _pick_target(mixed, last_target, rand, require_fpl_name=True)
        assert member.handle == "@has_name"  # the only member an fpl_name line can use
        assert member.fpl_name
        last_target = member.handle


# The pure `load_banter_state` loader (and the full State/save_state round trip) are
# tested in test_state.py, alongside every other State field's loader -- the same
# home decode_pending_greeting's tests have, for the same reason.
