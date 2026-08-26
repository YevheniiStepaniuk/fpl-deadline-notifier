"""A line of light banter, appended to a message. Pure: the owner's 20 lines, a
tolerant parser for the roster they get aimed at, and a picker with an injected
source of randomness so its rules -- no repeat within a cycle, no consecutive
repeat target -- are testable exactly like `schedule.py` tests its rules against an
injected `now` rather than the real clock.

Wiring (deciding *whether* to call any of this, and never letting a failure here
cost the alert it decorates) lives in `__main__.py`, not here -- the same split
`schedule.py`/`render.py` already have between "what is owed" and "how it looks
sent".
"""

import dataclasses
from collections.abc import Callable, Sequence


@dataclasses.dataclass(frozen=True)
class RosterMember:
    """One mate: the handle a line addresses, and the club that comes with them.

    A pair, not two independent fields picked apart -- `{name}` and `{team}` in a
    line always come from the *same* member, or "@romanusyk trusting Liverpool
    again" replaces "@romanusyk trusting Manchester United again" and the joke (that
    it is *his* team) is gone.
    """

    handle: str
    team: str


@dataclasses.dataclass(frozen=True)
class BanterLine:
    """One of the owner's 20 lines, plus what it needs to render.

    `id` is explicit and stable rather than the list index, because it is what
    `State.banter_used` persists across restarts -- reordering LINES below (to fix
    a typo, say) must not silently reset or corrupt every deployed cycle the way a
    positional id would.
    """

    id: str
    text: str
    needs_name: bool
    needs_team: bool  # implies needs_name; there is no team-only line


# The owner's words, verbatim -- not this module's to reword. `id` numbers match the
# spec's own 1-20 listing, so a report referencing "line 7" and this file agree.
LINES: tuple[BanterLine, ...] = (
    BanterLine("1", "Deadline approaching. Time to make a transfer based entirely on "
               "one YouTube thumbnail.", False, False),
    BanterLine("2", "Someone in this league is currently making a transfer they will "
               "spend the entire weekend defending.", False, False),
    BanterLine("3", "Another deadline, another opportunity to convince yourself that "
               "this time you've figured out FPL.", False, False),
    BanterLine("4", "Your bench has been outscoring your starting XI so consistently "
               "it deserves its own manager.", False, False),
    BanterLine("5", "The wildcard is not a panic button. Although historically, "
               "that's how it's been used.", False, False),
    BanterLine("6", "If your captain blanks, remember: you weren't wrong. Football "
               "was.", False, False),
    BanterLine("7", "{name} has once again mistaken owning good players for knowing "
               "how FPL works.", True, False),
    BanterLine("8", "{name}'s transfer history looks less like a strategy and more "
               "like a live reaction to Twitter.", True, False),
    BanterLine("9", "{name} has 15 players and somehow still managed to pick the "
               "wrong eleven.", True, False),
    BanterLine("10", "{name} is already preparing the \"I was unlucky\" explanation "
               "for Saturday.", True, False),
    BanterLine("11", "{name} spent the entire week planning their transfer. "
               "Excellent. The player will now score 1 point.", True, False),
    BanterLine("12", "{name} has activated the wildcard. The plan has officially "
               "entered its \"let's see what happens\" phase.", True, False),
    BanterLine("13", "{name} trusting {team} again. Bold.", True, True),
    BanterLine("14", "{name} has backed {team}. Please respect their commitment to "
               "making the same mistake twice.", True, True),
    BanterLine("15", "{name} is relying on {team} this week. Thoughts and prayers.",
               True, True),
    BanterLine("16", "{name} thinks {team} will save their gameweek. Football has "
               "other plans.", True, True),
    BanterLine("17", "{name} putting all their FPL faith in {team}. At this point "
               "it's less fantasy football and more religion.", True, True),
    BanterLine("18", "{name} captaining {team}. At least the decision was made. "
               "That's something.", True, True),
    BanterLine("19", "{name} saw {team}'s fixture and immediately forgot everything "
               "they've learned about FPL.", True, True),
    BanterLine("20", "{name} buying a {team} player after one good performance. "
               "Welcome to FPL.", True, True),
)

# Every id above, once, so a corrupt or foreign id in a loaded `banter_used` can be
# recognised as such -- see `state.py`'s loader.
LINE_IDS = frozenset(line.id for line in LINES)


def parse_roster(raw: str) -> tuple[RosterMember, ...]:
    """Parse `NOTIFIER_ROSTER`: comma-separated `handle:team` pairs.

    Tolerant on purpose, the same discipline `state.py` applies to a hand-edited
    state file: one malformed entry (no colon, a blank handle or team) is skipped
    rather than refusing the whole service to start over one mate's typo. Splits on
    the *first* colon only (`partition`, not `split`), so a team name that happens
    to contain one -- unlikely, but not this parser's business to assume -- still
    survives whole in `team`.
    """
    members = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            # Trailing comma, or the whole variable blank -- not an error, just
            # nothing to add. An unset/empty NOTIFIER_ROSTER must produce (), not a
            # single bogus entry from parsing "".
            continue
        handle, sep, team = entry.partition(":")
        if not sep:
            continue
        handle = handle.strip()
        team = team.strip()
        if not handle or not team:
            continue
        members.append(RosterMember(handle, team))
    return tuple(members)


def eligible_lines(roster: Sequence[RosterMember]) -> tuple[BanterLine, ...]:
    """Which lines are usable right now.

    With no roster, a line needing `{name}` has nothing to fill it with -- rather
    than raise or render a literal "{name}", the feature degrades to the six lines
    that need nothing at all. This is the one thing keeping an unconfigured roster
    from breaking banter outright, so `next_banter` leans on it rather than
    special-casing "no roster" itself.
    """
    if not roster:
        return tuple(line for line in LINES if not line.needs_name)
    return LINES


def _pick_target(
    roster: Sequence[RosterMember],
    last_target: str | None,
    rand: Callable[[int], int],
) -> RosterMember:
    candidates = [member for member in roster if member.handle != last_target]
    if not candidates:
        # Only reachable with a roster of exactly one, whose sole member *is*
        # last_target: excluding them would leave nothing to pick from at all. The
        # consecutive-target rule cannot be honoured here by construction -- there is
        # no second mate to switch to -- so the sensible compromise is to repeat
        # them rather than deadlock (no member to return) or raise (this is
        # decoration, not something worth failing an alert over). A roster of one
        # is exactly the case this can happen in; two or more always leaves at
        # least one candidate after excluding a single last_target.
        candidates = list(roster)
    return candidates[rand(len(candidates))]


def next_banter(
    used_ids: frozenset[str],
    last_target: str | None,
    roster: Sequence[RosterMember],
    rand: Callable[[int], int],
) -> tuple[str, frozenset[str], str | None]:
    """Pick and render one line. Pure -- `rand(n)` must return an index in `[0, n)`,
    injected the same way `now` is injected everywhere else in this codebase, so the
    no-repeat and no-consecutive-target rules below are provable in a test rather
    than merely probable across enough runs.

    Returns (rendered text, the used-ids set to persist, the target handle to
    persist as `last_target` -- None if this line targeted nobody).
    """
    pool = eligible_lines(roster)
    remaining = [line for line in pool if line.id not in used_ids]
    if not remaining:
        # Every eligible line has been used: the cycle is exhausted, so it starts
        # over. Deliberately not excluding the line that was *just* used -- "never
        # repeat a line until every eligible line has been used, then start the
        # cycle again" says nothing about the boundary between one cycle and the
        # next, and guarding it would need one more piece of state for a rule
        # nobody asked for.
        remaining = list(pool)
        used_ids = frozenset()
    line = remaining[rand(len(remaining))]
    new_used = used_ids | {line.id}

    if not line.needs_name:
        # No target: `eligible_lines` guarantees this whenever roster is empty, so
        # this branch is also what keeps an unconfigured roster from ever reaching
        # `_pick_target` with nothing to choose from.
        return line.text, new_used, None

    member = _pick_target(roster, last_target, rand)
    text = (
        line.text.format(name=member.handle, team=member.team)
        if line.needs_team
        else line.text.format(name=member.handle)
    )
    return text, new_used, member.handle
