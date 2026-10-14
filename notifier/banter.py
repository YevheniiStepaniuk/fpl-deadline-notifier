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
    """One mate: the handle a line addresses, the name they registered in the FPL
    league with, and the club that comes with them.

    A pair, not two (now three) independent fields picked apart -- `{name}` and
    `{team}` in a line always come from the *same* member, or "@bob_fpl trusting
    Liverpool again" replaces "@bob_fpl trusting Manchester United again" and the
    joke (that it is *his* team) is gone.

    `fpl_name` is a lookup key for a later feature (matching a manager in the
    league's standings by this name, to find their handle and eventually their
    squad) -- not out of scope here, just unused here. None of today's 20 lines
    render it, and it may be empty: a two-field `NOTIFIER_ROSTER` entry (the form
    already live in production) carries no fpl_name at all.
    """

    handle: str
    fpl_name: str
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
    # Defaulted, unlike the three above: no line in LINES sets this yet (the owner's
    # 20 are unchanged), so every existing positional BanterLine(...) call below
    # keeps working unchanged. Implies needs_name, same as needs_team -- there is
    # no fpl_name-only line either.
    needs_fpl_name: bool = False


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
    """Parse `NOTIFIER_ROSTER`: comma-separated entries, each either the old
    two-field `handle:team` or the new three-field `handle:fpl_name:team`.

    Tolerant on purpose, the same discipline `state.py` applies to a hand-edited
    state file: one malformed entry (no colon, a blank handle or team) is skipped
    rather than refusing the whole service to start over one mate's typo. The old
    two-field form is not a courtesy -- a container in production right now runs
    with it in its `.env`, and a parser that rejected it would silently degrade
    banter with nothing in the log to say why.

    Splits on the *first two* colons only (`split(..., maxsplit=2)`), never more:
    two pieces means `handle:team` (fpl_name defaults to ""), three means
    `handle:fpl_name:team`, and either way anything past the second colon stays
    whole in `team`, so a team name containing a colon of its own still survives.
    The one shape this cannot represent is an fpl_name containing a colon -- with
    only two splits to work with, a colon typed into fpl_name reads as the
    boundary before team instead, and the real team ends up glued onto the tail of
    fpl_name. Not worth a third split for a field that's realistically just a
    person's name.
    """
    members = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            # Trailing comma, or the whole variable blank -- not an error, just
            # nothing to add. An unset/empty NOTIFIER_ROSTER must produce (), not a
            # single bogus entry from parsing "".
            continue
        parts = entry.split(":", 2)
        if len(parts) == 2:
            handle, team = parts
            fpl_name = ""
        elif len(parts) == 3:
            handle, fpl_name, team = parts
        else:
            # No colon at all: neither the old nor the new form.
            continue
        handle = handle.strip()
        fpl_name = fpl_name.strip()
        team = team.strip()
        if not handle or not team:
            continue
        members.append(RosterMember(handle, fpl_name, team))
    return tuple(members)


def eligible_lines(roster: Sequence[RosterMember]) -> tuple[BanterLine, ...]:
    """Which lines are usable right now.

    With no roster, a line needing `{name}` has nothing to fill it with -- rather
    than raise or render a literal "{name}", the feature degrades to the six lines
    that need nothing at all. This is the one thing keeping an unconfigured roster
    from breaking banter outright, so `next_banter` leans on it rather than
    special-casing "no roster" itself.

    Same idea, one field narrower: if nobody in the roster has an fpl_name filled
    in (the two-field `NOTIFIER_ROSTER` form, still live in production, leaves it
    ""), a line needing `{fpl_name}` degrades out too. No line in LINES sets
    needs_fpl_name yet, so this branch is inert today -- it exists so a future
    line built on {fpl_name} is safe to add without also touching this function.
    """
    if not roster:
        return tuple(line for line in LINES if not line.needs_name)
    if not any(member.fpl_name for member in roster):
        return tuple(line for line in LINES if not line.needs_fpl_name)
    return LINES


def _pick_target(
    roster: Sequence[RosterMember],
    last_target: str | None,
    rand: Callable[[int], int],
    *,
    require_fpl_name: bool = False,
) -> RosterMember:
    # A line needing {fpl_name} must never render a blank one, so it can only
    # target a member who actually has one -- narrow the pool *before* applying
    # the no-repeat rule below, so "no one else to switch to" is judged against
    # the members who qualify at all, not the full roster. `eligible_lines`
    # guarantees this pool is non-empty whenever such a line is even reachable
    # here (see its own docstring), so `pool` itself is always safe to fall back
    # to below.
    pool = [member for member in roster if member.fpl_name] if require_fpl_name else list(roster)
    candidates = [member for member in pool if member.handle != last_target]
    if not candidates:
        # Only reachable with a (possibly fpl_name-narrowed) pool of exactly one,
        # whose sole member *is* last_target: excluding them would leave nothing to
        # pick from at all. The consecutive-target rule cannot be honoured here by
        # construction -- there is no second mate to switch to -- so the sensible
        # compromise is to repeat them rather than deadlock (no member to return)
        # or raise (this is decoration, not something worth failing an alert over).
        # Falling back to `pool`, not the full roster, keeps the fpl_name guarantee
        # above intact even on this path.
        candidates = pool
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

    member = _pick_target(roster, last_target, rand, require_fpl_name=line.needs_fpl_name)
    # Every kwarg is passed unconditionally rather than branching per need (as this
    # used to, before fpl_name made it a three-way branch): str.format ignores a
    # kwarg its template doesn't reference, so this is exactly equivalent to the
    # old name-only/name+team split, just without a branch per new field added.
    text = line.text.format(name=member.handle, team=member.team, fpl_name=member.fpl_name)
    return text, new_used, member.handle
