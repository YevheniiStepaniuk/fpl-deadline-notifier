"""The AI version of the sting: one line about a real manager, written from real data.

`banter.py` holds the owner's twenty fixed lines and knows nothing about anybody's
actual season. This module is the same job done from `league.py`'s live snapshots --
the table, and crucially the latest transfers and waiver claims, so the joke can be
about the move somebody made this week rather than about FPL in general.

Split the same way the rest of the codebase splits: everything decidable is a pure
function taking `now`/`rand` as arguments (`choose_target`, `describe`,
`build_messages`, `clean_line`, `ai_allowed`), and exactly one function
(`generate_line`) touches the network. `__main__.py` owns the decision to call any of
it and the guarantee that a failure here cannot cost an alert.
"""

import dataclasses
import datetime
import json
import logging
import re
from collections.abc import Callable, Sequence

import httpx

from notifier.banter import RosterMember
from notifier.league import LeagueSnapshot, Move, Standing

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

TIMEOUT = 30.0

# One line, appended under an alert that has to stay scannable. Long enough for a
# setup and a punchline, short enough that it cannot push the deadline itself off a
# phone screen.
MAX_LENGTH = 240

# How long after an AI line before another is worth paying for. `/nextdeadline` is
# answerable by anyone in the group chat, so without this a bored mate can spend the
# operator's OpenRouter credit as fast as they can type. Inside the window the static
# lines answer instead, which is indistinguishable from the feature being off.
COOLDOWN = datetime.timedelta(minutes=15)

# Generous for a one-line answer, and deliberately so. A reasoning model bills its
# thinking against this budget before it writes anything, so 200 -- ample for the line
# itself -- is where deepseek/deepseek-v4-pro-0813 returns `content: null` having spent
# the lot on reasoning. Unused tokens cost nothing; a budget that runs out costs the
# whole line.
MAX_TOKENS = 300

# Moves quoted per manager in the prompt. The target gets their own, everyone else is
# there so the model can compare -- a joke about being outbid needs the other bid.
MOVES_PER_MANAGER = 4

log = logging.getLogger("notifier")


class BanterError(Exception):
    """Raised when OpenRouter cannot produce a usable line. Caught by the caller."""


@dataclasses.dataclass(frozen=True)
class Target:
    """Who the line is about, and how to address them.

    `label` is what the joke says: a Telegram handle when the roster could be matched
    to this league row (so the mention notifies them), otherwise the manager's own name
    off the API. `handle` is what gets persisted as `banter_last_target`, and is None
    for an unmatched manager -- the alternation rule then falls back to the label,
    which is stable for as long as they do not rename their team.
    """

    label: str
    handle: str | None
    team_name: str
    entry: int | None
    game: str
    # One id per game. The two games issue a manager a *different* entry id, and moves
    # are keyed by the id of the game that published them -- so a single `entry` was
    # enough to file the target's own draft waiver under "other managers' moves" while
    # the prompt said they had made none. Observed live on a real four-manager league. Defaulted to ()
    # so a Target built with just `entry` (every test that predates this) still works,
    # and `entry_for` falls back to it.
    entries: tuple[tuple[str, int], ...] = ()

    @property
    def key(self) -> str:
        return self.handle or self.label

    def entry_for(self, game: str) -> int | None:
        """This manager's id in `game`, or None if they do not play it.

        Falls back to `entry` only when no per-game ids were recorded at all; returning
        it for the *wrong* game is the bug this method exists to stop, since a
        coincidental id collision across the two APIs would misattribute a move.
        """
        for known_game, entry in self.entries:
            if known_game == game:
                return entry
        return self.entry if not self.entries else None


def parse_aliases(raw: str) -> tuple[tuple[str, str], ...]:
    """Parse `NOTIFIER_BANTER_ALIASES`: comma-separated `@handle=Name As The API Has It`.

    `=` rather than `:` as the separator, unlike NOTIFIER_ROSTER, because a manager name
    is free text somebody typed into a football website and a colon in one is likelier
    than an equals sign. Splits on the first `=` only.

    Tolerant in the same way `banter.parse_roster` is: a malformed pair is skipped
    rather than refused, since the cost is one mate losing their mention, not a service
    that will not start. Exists because handle-to-name guessing has a hard floor --
    nothing links "@nine_iron" to "Ada Lovelace" -- and this is the only way
    to close it.
    """
    pairs = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        handle, sep, name = entry.partition("=")
        if not sep:
            continue
        handle, name = handle.strip(), name.strip()
        if not handle or not name:
            continue
        pairs.append((handle, name))
    return tuple(pairs)


def _normalise(text: str) -> str:
    """Lowercase alphanumerics only.

    `@just_bergs` and "Just Bergs" and "just-yuricle FC" all have to collapse to
    the same thing, because a roster handle and an FPL team name are typed by the same
    person on two different days and agree on nothing but the letters.

    Underscores, dots and hyphens become spaces rather than vanishing, so a handle's own
    word boundaries survive for `_matches`' per-word pass; everything else non-alphanumeric
    is dropped.
    """
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"[_.\-]+", " ", text.lower())).strip()


# The reverse direction of the match below needs a floor. A handle contains a name
# ("@rexmarlow" contains "roman") often enough to be worth checking, but a two- or
# three-letter fragment is inside somebody's handle by chance, and the cost of a chance
# match is a personal joke aimed at the wrong mate.
_MIN_REVERSE_MATCH = 4


def _matches(needle: str, hay: str) -> bool:
    """Whether a normalised handle and a normalised name are the same person.

    Whole-string containment first, then per-word: "@t_harrington" and "Tomas
    Harrington" share no whole string ("tharrington" is not inside "tomasharrington")
    but do share a surname, and a surname long enough to clear the floor is about as
    strong a signal as this can get without a configured mapping.
    """
    if not needle or not hay:
        return False
    if needle in hay or (len(hay) >= _MIN_REVERSE_MATCH and hay in needle):
        return True
    return any(
        len(word) >= _MIN_REVERSE_MATCH and word in needle
        for word in hay.split()
    )


def _words(text: str) -> list[str]:
    return [word for word in _normalise(text).split() if len(word) >= _MIN_REVERSE_MATCH]


def link_roster(
    table: Sequence[Standing],
    roster: Sequence[RosterMember],
    aliases: Sequence[tuple[str, str]] = (),
) -> dict[int, RosterMember]:
    """Match roster handles to league rows, by entry id.

    Deliberately conservative: a handle is linked only when its letters appear in the
    manager's name or team name (or vice versa, for a handle longer than the name).
    Guessing wrong is worse than not guessing -- an unlinked manager is still roasted,
    just addressed by their real name instead of a mention, whereas a bad link aims a
    personal joke at the wrong person and tags them in it.

    `aliases` -- `NOTIFIER_BANTER_ALIASES`, handle to manager name -- is checked first
    and in two passes, because guessing has a hard floor: no amount of letter-matching
    links "@nine_iron" to "Ada Lovelace". Pass one is exact on the normalised
    full name. Pass two, for rows pass one left alone, accepts a single shared word of
    four letters or more, so a configured name that disagrees with the API on spelling
    ("Tomas Harington" against the API's "Tomas Harrington" -- the real case this was
    written for) still lands. Exact-full-name always wins, so two mates sharing a first
    name resolve to whoever spelled their surname the way the API does.

    Mapping to entry ids instead was considered and rejected: an id has to be dug out of
    an API response, while the name is on the page the operator is already looking at.
    """
    by_handle = {handle: name for handle, name in aliases}
    linked: dict[int, RosterMember] = {}

    for exact in (True, False):
        for row in table:
            if row.entry is None or row.entry in linked:
                continue
            for member in roster:
                alias = by_handle.get(member.handle)
                if not alias:
                    continue
                if exact:
                    hit = _normalise(alias) == _normalise(row.manager)
                else:
                    alias_words, row_words = set(_words(alias)), set(_words(row.manager))
                    hit = bool(alias_words & row_words)
                if hit:
                    linked[row.entry] = member
                    break

    for row in table:
        if row.entry is None or row.entry in linked:
            # A configured alias is never overridden by a letter-match guess.
            continue
        haystacks = [_normalise(row.manager), _normalise(row.team_name)]
        for member in roster:
            # Spaces stripped on this side only: the handle is one token to be
            # searched *in*, while the name keeps its words to be searched *for*.
            needle = _normalise(member.handle).replace(" ", "")
            if not needle:
                continue
            if any(_matches(needle, hay) for hay in haystacks):
                linked[row.entry] = member
                break
    return linked


def candidates(
    snapshots: Sequence[LeagueSnapshot],
    roster: Sequence[RosterMember],
    aliases: Sequence[tuple[str, str]] = (),
) -> tuple[Target, ...]:
    """Everyone who could be the butt of the joke, deduplicated across both games.

    A manager playing both classic and draft is one person and must not get two
    entries in the pick -- that would make them twice as likely to be targeted as the
    mate who only plays one. Deduplication is by label, which is the handle where one
    was linked and so survives the two games naming them differently.
    """
    targets: dict[str, Target] = {}
    for snapshot in snapshots:
        linked = link_roster(snapshot.table, roster, aliases)
        for row in snapshot.table:
            member = linked.get(row.entry) if row.entry is not None else None
            label = member.handle if member else (row.manager or row.team_name)
            if not label:
                # Nothing to call them. A row with neither name is not worth a joke.
                continue
            found = targets.get(label)
            pair = ((snapshot.game, row.entry),) if row.entry is not None else ()
            if found is None:
                targets[label] = Target(
                    label=label,
                    handle=member.handle if member else None,
                    team_name=row.team_name,
                    entry=row.entry,
                    game=snapshot.game,
                    entries=pair,
                )
                continue
            # Same person, second game: merge rather than skip, so the ids of both are
            # available and each snapshot's moves can be attributed to the right one.
            # The first game seen stays the primary `entry`/`game`/`team_name`.
            targets[label] = dataclasses.replace(
                found,
                entries=found.entries + tuple(p for p in pair if p not in found.entries),
                handle=found.handle or (member.handle if member else None),
                team_name=found.team_name or row.team_name,
            )
    return tuple(targets.values())


def choose_target(
    snapshots: Sequence[LeagueSnapshot],
    roster: Sequence[RosterMember],
    last_target: str | None,
    rand: Callable[[int], int],
    aliases: Sequence[tuple[str, str]] = (),
) -> Target | None:
    """Pick who this line is about, or None if there is nobody to pick.

    Random rather than "whoever is last", so nobody becomes the league's designated
    punchline for the whole season, and never the same person twice running -- the same
    rule `banter.py` enforces for its own targets, with the same single-member escape
    hatch: a league of one repeats them rather than returning None.
    """
    pool = candidates(snapshots, roster, aliases)
    if not pool:
        return None
    fresh = [target for target in pool if target.key != last_target] or list(pool)
    return fresh[rand(len(fresh))]


def _move_line(move: Move) -> str:
    verb = {"waiver": "waiver claim", "free agent": "free-agent pickup", "transfer": "transfer"}.get(
        move.kind, move.kind
    )
    outcome = "" if move.accepted else " -- REJECTED, lost the waiver"
    return f"GW{move.gw} {verb}: in {move.player_in}, out {move.player_out}{outcome}"


def _table_line(row: Standing) -> str:
    bits = [f"{row.rank}." if row.rank is not None else "-", row.manager or "?"]
    if row.team_name:
        bits.append(f"({row.team_name})")
    if row.total_points is not None:
        bits.append(f"{row.total_points} pts total, {row.gw_points} this GW")
    if row.record is not None:
        bits.append(f"W-D-L {row.record}, {row.league_points} league pts, {row.points_for} scored")
    return " ".join(bits)


def describe(snapshots: Sequence[LeagueSnapshot], target: Target) -> str:
    """The facts, as plain text rather than raw JSON.

    Prose costs a third of the tokens the equivalent JSON does and, in testing, gets
    the model to quote the numbers rather than invent neighbouring ones. The target's
    own moves are pulled out under their own heading because a line about "the latest
    waiver" must not accidentally describe somebody else's.
    """
    blocks = []
    for snapshot in snapshots:
        if snapshot.game == "fpl":
            game = "Fantasy Premier League (classic)"
        elif snapshot.scoring == "h2h":
            game = "Draft PL (head-to-head)"
        else:
            # A Draft PL league can score like a classic one: no matches, no W-D-L, the
            # table is accumulated points. Saying "head-to-head" here invites a joke
            # about a fixture that was never played.
            game = "Draft PL (classic scoring, drafted squads, no head-to-head)"
        lines = [f"{game} -- league \"{snapshot.league_name}\"", "Table:"]
        lines.extend(f"  {_table_line(row)}" for row in snapshot.table)

        entry = target.entry_for(snapshot.game)
        own = [m for m in snapshot.moves if entry is not None and m.entry == entry]
        others = [m for m in snapshot.moves if m not in own]
        if own:
            lines.append(f"{target.label}'s latest moves (newest first):")
            lines.extend(f"  {_move_line(m)}" for m in own[:MOVES_PER_MANAGER])
        else:
            lines.append(f"{target.label} has made no transfers or claims here yet.")
        if others:
            lines.append("Other managers' latest moves:")
            by_entry = {row.entry: (row.manager or row.team_name) for row in snapshot.table}
            lines.extend(
                f"  {by_entry.get(m.entry, 'someone')}: {_move_line(m)}"
                for m in others[:MOVES_PER_MANAGER]
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


SYSTEM_PROMPT = """You write one line of banter for a WhatsApp-style group of friends \
who play Fantasy Premier League and Draft PL against each other. The tone is a mate \
taking the piss: sarcastic, cutting, deadpan, funny.

Rules, all of them hard:
- Write about the named target only, and hang the joke on a specific fact from the \
data: their latest transfer or waiver claim, their rank, their points, a player they \
own, a claim they lost.
- Address the target by the exact label given, verbatim, somewhere in the line. When \
that label is an @handle, the line must contain that @handle -- it is a group-chat \
mention and is how they find out the joke is about them. Do not substitute "mate", \
"someone", their team name, or a nickname for it.
- If the target has a most recent transfer or waiver move in the data, the line MUST \
be about that move.
- Never invent a fact. No number, player or position that is not in the data.
- Mock their football decisions, their table position, their taste in players, their \
self-belief. Never their race, ethnicity, religion, nationality, gender, sexuality, \
disability, income, family or health -- nor their appearance, body, hair, height \
or weight. This is football-shaming between \
friends, not abuse.
- One line. Under 200 characters. No emoji, no hashtags, no quotation marks around it, \
no preamble, no explanation. Output the line and nothing else."""


def build_messages(
    snapshots: Sequence[LeagueSnapshot], target: Target, now: datetime.datetime
) -> list[dict[str, str]]:
    """The OpenRouter `messages` array. Pure, so a test can assert on what was sent."""
    user = (
        f"Target: {target.label}"
        + (f" (team \"{target.team_name}\")" if target.team_name else "")
        + f"\nDate: {now.date().isoformat()}\n\n{describe(snapshots, target)}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


_WRAPPERS = "\"'`*_ "


def clean_line(raw: str) -> str:
    """One line, no wrappers, bounded length.

    A model asked for one line occasionally returns two, or wraps it in quotes, or
    leads with "Here you go:". The message this gets appended to is plain text with no
    parse mode, so nothing here has to be escaped -- only flattened, since a stray
    newline would silently split the sting from the alert it belongs to.
    """
    text = " ".join(raw.replace("\r", "\n").split("\n")[0].split()).strip(_WRAPPERS)
    # A leading label ("Line:", "Banter:") survives the strip above but not this.
    text = re.sub(r"^(here(?:'s| is)[^:]*|line|banter|joke|output)\s*:\s*", "", text, flags=re.I).strip(_WRAPPERS)
    if not text:
        raise BanterError("model returned an empty line")
    if len(text) > MAX_LENGTH:
        # Cut at a sentence end where there is one, so a truncated line still reads as
        # a finished joke rather than trailing off mid-word.
        cut = text[:MAX_LENGTH]
        stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[: stop + 1] if stop > MAX_LENGTH // 2 else cut.rstrip() + "..."
    return text


def ai_allowed(last_at: datetime.datetime | None, now: datetime.datetime) -> bool:
    """Whether enough time has passed since the last AI line to pay for another.

    A `last_at` in the future -- a clock stepped backwards, a hand-edited state file --
    is treated as "just now" rather than as a gap wide enough to spend against. Signed
    comparison alone would read an hour in the future as an hour elapsed and permit the
    call; the explicit check costs one static line instead of opening the tap.
    """
    if last_at is None:
        return True
    if last_at > now:
        return False
    return now - last_at >= COOLDOWN


def generate_line(
    client: httpx.Client,
    api_key: str,
    model: str,
    snapshots: Sequence[LeagueSnapshot],
    target: Target,
    now: datetime.datetime,
) -> str:
    """Ask OpenRouter for the line. The only impure function in this module.

    Raises BanterError on anything unusable -- non-200, a body shaped differently than
    expected, an empty completion. The caller turns that into a static line, so the
    exception type is the whole interface: no partial success, no None to check.
    """
    body = {
        "model": model,
        "messages": build_messages(snapshots, target, now),
        # Hot on purpose: four alerts a gameweek about the same handful of managers
        # from near-identical data, and a cold model writes the same joke every time.
        "temperature": 1.0,
        "max_tokens": MAX_TOKENS,
        # Sent to every model, reasoning or not. OpenRouter drops the key for models
        # that have no such mode (verified against anthropic/claude-sonnet-5 and
        # openai/gpt-4o-mini, both 200), and on one that does it is the difference
        # between $0.0002 and $0.0027 a line -- for a single sarcastic sentence about a
        # transfer, thinking first buys nothing.
        "reasoning": {"enabled": False},
    }
    try:
        response = client.post(
            OPENROUTER_URL,
            json=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # OpenRouter attributes traffic by these; they are optional but make
                # the spend legible on the dashboard next to the operator's other keys.
                "X-Title": "fpl-deadline-notifier",
                "HTTP-Referer": "https://github.com/fpl-deadline-notifier",
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise BanterError(f"OpenRouter request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BanterError(f"OpenRouter returned non-JSON: {exc}") from exc

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise BanterError(f"OpenRouter response has no usable choice: {payload!r:.200}") from exc
    if not isinstance(content, str):
        # The reasoning-model failure mode: a completion that spent its whole token
        # budget thinking and returned null. Named explicitly because the fix is a
        # setting (MAX_TOKENS, or the model itself), not a retry.
        raise BanterError(
            f"OpenRouter content was {type(content).__name__}, not a string "
            f"(finish_reason={payload.get('choices', [{}])[0].get('finish_reason')!r}, "
            f"usage={payload.get('usage')})"
        )
    return clean_line(content)
