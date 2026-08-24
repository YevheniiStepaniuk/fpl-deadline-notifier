"""The two official endpoints, normalised to `Moment`.

Both were called unauthenticated on 2026-08-22 and both answered. Deadlines are
properties of the game rather than of an entry, so there is no team id, no league id
and no cookie to keep alive -- which is the whole reason the Draft waiver alert is
cheap enough to be worth having.
"""

import dataclasses
import datetime
from collections.abc import Iterable

import httpx

FPL_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
DRAFT_URL = "https://draft.premierleague.com/api/bootstrap-static"

TIMEOUT = 15.0


@dataclasses.dataclass(frozen=True)
class Moment:
    """A single instant something is due, in UTC.

    `games` is a set rather than a string because the two games publish the same
    gameweek deadline and a merged moment carries both.
    """

    kind: str  # "deadline" | "waivers"
    gw: int
    when: datetime.datetime  # timezone-aware, UTC
    games: frozenset[str]


def _parse_time(raw: str | None) -> datetime.datetime | None:
    # Draft leaves waivers_time null for gameweeks whose window is not scheduled yet.
    # Crashing on one of those during a refresh would take out unrelated pending alerts.
    if not raw:
        return None
    return datetime.datetime.fromisoformat(raw).astimezone(datetime.UTC)


def parse_fpl(payload: dict) -> list[Moment]:
    events = payload.get("events")
    # Not `.get("events", [])`. An empty list is indistinguishable from "this game has
    # no deadlines", which is the exact misreading fetch_source raises to avoid: the
    # caller would cache the emptiness, overwrite its last-good copy and go quiet with
    # nothing logged. A 200 carrying a maintenance page arrives here looking like this.
    if not isinstance(events, list) or not events:
        raise ValueError(f"FPL payload has no usable `events` list: {type(events).__name__}")
    moments = []
    for event in events:
        when = _parse_time(event.get("deadline_time"))
        if when is not None:
            moments.append(Moment("deadline", event["id"], when, frozenset({"fpl"})))
    return moments


def parse_draft(payload: dict) -> list[Moment]:
    # Draft nests the list one level deeper than classic does: `events` is a dict of
    # current/next/data rather than the list itself. Reusing parse_fpl here raises
    # TypeError, which is why the two functions exist separately.
    events = payload.get("events")
    data = events.get("data") if isinstance(events, dict) else None
    if not isinstance(data, list) or not data:
        raise ValueError(f"Draft payload has no usable `events.data` list: {type(data).__name__}")
    moments = []
    for event in data:
        for kind, field in (("deadline", "deadline_time"), ("waivers", "waivers_time")):
            when = _parse_time(event.get(field))
            if when is not None:
                moments.append(Moment(kind, event["id"], when, frozenset({"draft"})))
    return moments


_SOURCES = {"fpl": (FPL_URL, parse_fpl), "draft": (DRAFT_URL, parse_draft)}


def fetch_source(client: httpx.Client, game: str) -> list[Moment]:
    """Fetch and parse one game's deadlines. Raises on any failure: httpx.HTTPError from
    the transport, ValueError from a payload with no usable `events`.

    Raising rather than returning [] is load-bearing: the caller falls back to its
    cached copy, and an empty list would instead read as "this game has no deadlines"
    and quietly retire every alert still pending for it.
    """
    if game not in _SOURCES:
        raise ValueError(f"unknown game {game!r}; expected one of {sorted(_SOURCES)}")
    url, parse = _SOURCES[game]
    response = client.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return parse(response.json())


def merge(*groups: Iterable[Moment]) -> list[Moment]:
    """Combine per-source moments, unioning `games` where kind, gw and instant match.

    Equality is exact. The two games have published different deadlines for the same
    gameweek before, and rounding them together would announce the earlier one at the
    later one's time -- the exact failure this service exists to prevent.
    """
    by_identity: dict[tuple[str, int, datetime.datetime], set[str]] = {}
    for group in groups:
        for moment in group:
            key = (moment.kind, moment.gw, moment.when)
            by_identity.setdefault(key, set()).update(moment.games)
    moments = [
        Moment(kind, gw, when, frozenset(games))
        for (kind, gw, when), games in by_identity.items()
    ]
    return sorted(moments, key=lambda m: (m.when, m.kind, m.gw))
