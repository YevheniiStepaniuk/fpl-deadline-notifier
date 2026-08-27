"""Live mini-league state from both official APIs, normalised for the banter prompt.

`sources.py` fetches deadlines, which are properties of the *game* -- no league, no
entry, no cookie. This module is the opposite: everything here is keyed by a league id
the operator supplies, and it exists only to give the AI banter something true to be
rude about. Kept separate from `sources.py` for that reason -- a failure here costs a
punchline, a failure there costs an alert.

All endpoints below were called unauthenticated on 2026-08-27 and answered:

    /api/leagues-classic/{id}/standings/     classic table
    /api/entry/{id}/transfers/               one classic manager's transfers, all season
    /api/league/{id}/details                 draft table, entries, H2H matches
    /api/draft/league/{id}/transactions      every waiver and free-agent move in the league

Draft's *per-entry* transaction endpoint (`/api/draft/entry/{id}/transactions`) answers
403 without a session cookie, which is why the league-level one is used instead: it
carries the same moves for everybody at once and needs no login.
"""

import dataclasses
import datetime
import logging

import httpx

FPL_API = "https://fantasy.premierleague.com/api"
DRAFT_API = "https://draft.premierleague.com/api"

TIMEOUT = 15.0

# How many managers' classic transfer histories to pull. Classic publishes transfers
# per entry, so a table of N costs N requests; a mini-league of mates is far under this
# and a leaderboard someone pasted the id of by mistake is not worth 500 requests.
MAX_TRANSFER_FETCHES = 12

# How many recent moves survive into the prompt, per league. Newest first. The point is
# "what did you just do", not a season audit, and an unbounded list would be most of
# the prompt by GW20.
RECENT_MOVES = 10

log = logging.getLogger("notifier")


@dataclasses.dataclass(frozen=True)
class Standing:
    """One row of a league table, in whichever terms that game keeps score.

    `gw_points`/`total_points` are the classic currency; `record` ("2-1-0") and
    `league_points` are draft's. Both games fill `manager`/`team_name`, and neither is
    guaranteed non-empty -- a manager who never renamed their team still has a name,
    but the field is other people's data and not worth crashing a joke over.
    """

    entry: int | None
    manager: str
    team_name: str
    rank: int | None
    gw_points: int | None = None
    total_points: int | None = None
    record: str | None = None
    league_points: int | None = None
    points_for: int | None = None


@dataclasses.dataclass(frozen=True)
class Move:
    """One transfer (classic) or waiver/free-agent claim (draft).

    `accepted` is only ever False on the draft side: a waiver claim can lose to a
    higher priority and be rejected, which is funnier than a successful one and the
    single best reason to bother fetching moves at all.
    """

    entry: int | None
    gw: int | None
    player_in: str
    player_out: str
    kind: str  # "transfer" | "waiver" | "free agent"
    accepted: bool = True
    when: str | None = None  # ISO8601 as published, draft only


@dataclasses.dataclass(frozen=True)
class LeagueSnapshot:
    game: str  # "fpl" | "draft"
    league_id: int
    league_name: str
    table: tuple[Standing, ...]
    moves: tuple[Move, ...]  # newest first, at most RECENT_MOVES
    # How the league keeps score: "h2h" or "classic". Only ever meaningful for draft --
    # a Draft PL league can be either, and the two publish *different standings fields*
    # (see parse_draft_table). Classic FPL leagues are always "classic". Defaulted so
    # existing construction of this dataclass keeps working.
    scoring: str = "classic"


def _get(client: httpx.Client, url: str) -> dict | list:
    response = client.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def element_names(bootstrap: dict) -> dict[int, str]:
    """Player id -> short display name, for either game's bootstrap.

    Both games publish `elements` with a `web_name`, so one function serves both. A
    missing name is skipped rather than defaulted: `_name` below renders an unknown id
    as "someone", which reads better in a joke than "None" or a bare number.
    """
    elements = bootstrap.get("elements")
    if not isinstance(elements, list):
        return {}
    return {
        element["id"]: element.get("web_name") or ""
        for element in elements
        if isinstance(element, dict) and element.get("id") is not None
    }


def _name(names: dict[int, str], element_id: object) -> str:
    if not isinstance(element_id, int):
        return "someone"
    return names.get(element_id) or "someone"


# ------------------------------------------------------------------ classic (FPL)


def parse_classic_table(payload: dict) -> tuple[str, tuple[Standing, ...]]:
    """The classic standings payload -> league name and rows.

    Raises on a payload with no rows, for the same reason `sources.parse_fpl` does: an
    empty table is indistinguishable from a maintenance page returned with a 200, and
    the caller's fallback (the static banter lines) is a better answer than a prompt
    describing a league of nobody.
    """
    results = payload.get("standings", {}).get("results") if isinstance(payload.get("standings"), dict) else None
    if not isinstance(results, list) or not results:
        raise ValueError("classic standings payload has no usable `standings.results` list")
    rows = tuple(
        Standing(
            entry=row.get("entry"),
            manager=(row.get("player_name") or "").strip(),
            team_name=(row.get("entry_name") or "").strip(),
            rank=row.get("rank"),
            gw_points=row.get("event_total"),
            total_points=row.get("total"),
        )
        for row in results
        if isinstance(row, dict)
    )
    name = (payload.get("league", {}) or {}).get("name") or f"league {payload.get('league', {}).get('id')}"
    return name, rows


def parse_classic_transfers(payload: object, names: dict[int, str], entry: int | None) -> tuple[Move, ...]:
    """One entry's transfer list -> moves, newest first.

    The endpoint returns a bare list, newest first already, and an empty list is
    normal rather than suspect: before the first deadline of the season nobody has
    transferred anybody.
    """
    if not isinstance(payload, list):
        return ()
    return tuple(
        Move(
            entry=entry,
            gw=row.get("event"),
            player_in=_name(names, row.get("element_in")),
            player_out=_name(names, row.get("element_out")),
            kind="transfer",
            when=row.get("time"),
        )
        for row in payload
        if isinstance(row, dict)
    )


def fetch_classic(client: httpx.Client, league_id: int) -> LeagueSnapshot:
    """Classic table plus each manager's recent transfers. Raises if the table fails.

    Transfers are best-effort per manager: one entry answering 404 (a deleted team) or
    timing out costs that manager's moves, not the snapshot. The table is what the
    prompt cannot do without.
    """
    league_name, table = parse_classic_table(_get(client, f"{FPL_API}/leagues-classic/{league_id}/standings/"))
    names = element_names(_get(client, f"{FPL_API}/bootstrap-static/"))

    moves: list[Move] = []
    for row in table[:MAX_TRANSFER_FETCHES]:
        if row.entry is None:
            continue
        try:
            payload = _get(client, f"{FPL_API}/entry/{row.entry}/transfers/")
        except Exception as exc:
            log.info("classic transfers for entry %s skipped: %s", row.entry, exc)
            continue
        moves.extend(parse_classic_transfers(payload, names, row.entry))

    # Sorted across managers, newest first. `when` is an ISO8601 string in UTC for
    # every row the API publishes, so a lexical sort is a chronological one; a row
    # missing it sinks to the bottom rather than raising on a None comparison.
    moves.sort(key=lambda m: (m.when or "", m.gw or 0), reverse=True)
    return LeagueSnapshot("fpl", league_id, league_name, table, tuple(moves[:RECENT_MOVES]))


# ------------------------------------------------------------------------- draft


def parse_draft_table(payload: dict) -> tuple[str, tuple[Standing, ...], dict[int, int], str]:
    """The draft league details payload -> name, rows, league_entry -> entry_id, scoring.

    Draft keeps two ids per manager: `league_entry` (their row in *this* league) and
    `entry_id` (their team, league-independent). Standings and matches reference the
    former, transactions the latter, so the mapping between them has to come back out
    of here or the moves cannot be attributed to anybody.
    """
    entries = payload.get("league_entries")
    standings = payload.get("standings")
    if not isinstance(entries, list) or not isinstance(standings, list) or not standings:
        raise ValueError("draft details payload has no usable `standings`/`league_entries`")

    by_league_entry = {e.get("id"): e for e in entries if isinstance(e, dict)}
    rows = []
    for row in standings:
        if not isinstance(row, dict):
            continue
        entry = by_league_entry.get(row.get("league_entry")) or {}
        manager = " ".join(
            part for part in (entry.get("player_first_name"), entry.get("player_last_name")) if part
        ).strip()
        won, drawn, lost = row.get("matches_won"), row.get("matches_drawn"), row.get("matches_lost")
        h2h = None not in (won, drawn, lost)
        # The same `total` key means two different things depending on the mode: league
        # points from won/drawn matches in a head-to-head league, and plain accumulated
        # score in a classic-scoring one. Reading it as the wrong one puts "53 league
        # pts" next to a league that plays no matches -- a number the model would then
        # be rude about having invented.
        rows.append(
            Standing(
                entry=entry.get("entry_id"),
                manager=manager,
                team_name=(entry.get("entry_name") or "").strip(),
                rank=row.get("rank"),
                gw_points=None if h2h else row.get("event_total"),
                total_points=None if h2h else row.get("total"),
                record=f"{won}-{drawn}-{lost}" if h2h else None,
                league_points=row.get("total") if h2h else None,
                points_for=row.get("points_for") if h2h else None,
            )
        )
    name = (payload.get("league", {}) or {}).get("name") or f"league {payload.get('league', {}).get('id')}"
    # `scoring` is Draft's own single letter: "h" for head-to-head, "c" for classic.
    # Taken from the payload rather than inferred from the rows, so an empty table at
    # the very start of a season still labels the league correctly.
    scoring = "h2h" if (payload.get("league", {}) or {}).get("scoring") == "h" else "classic"
    return name, tuple(rows), {
        e.get("id"): e.get("entry_id") for e in entries if isinstance(e, dict) and e.get("entry_id")
    }, scoring


# Draft's own single letters, spelled out for the prompt. Anything unrecognised keeps
# the raw code rather than being dropped -- a new kind appearing is worth seeing in a
# log or a joke, not silently becoming "transfer".
_DRAFT_KINDS = {"w": "waiver", "f": "free agent", "t": "trade"}


def parse_draft_transactions(payload: dict, names: dict[int, str]) -> tuple[Move, ...]:
    """Every waiver and free-agent move in the league -> moves, newest first.

    `result` is "a" for an accepted claim; anything else (notably "d") lost to a higher
    waiver priority. Rejected claims are kept deliberately -- being outbid on a waiver
    is the single most mockable thing in the payload.
    """
    rows = payload.get("transactions")
    if not isinstance(rows, list):
        return ()
    moves = [
        Move(
            entry=row.get("entry"),
            gw=row.get("event"),
            player_in=_name(names, row.get("element_in")),
            player_out=_name(names, row.get("element_out")),
            kind=_DRAFT_KINDS.get(row.get("kind"), str(row.get("kind"))),
            accepted=row.get("result") == "a",
            when=row.get("added"),
        )
        for row in rows
        if isinstance(row, dict)
    ]
    moves.sort(key=lambda m: (m.when or "", m.gw or 0), reverse=True)
    return tuple(moves)


def fetch_draft(client: httpx.Client, league_id: int) -> LeagueSnapshot:
    """Draft table plus the league's recent waiver traffic. Raises if the table fails."""
    league_name, table, _, scoring = parse_draft_table(
        _get(client, f"{DRAFT_API}/league/{league_id}/details")
    )
    names = element_names(_get(client, f"{DRAFT_API}/bootstrap-static"))

    moves: tuple[Move, ...] = ()
    try:
        moves = parse_draft_transactions(_get(client, f"{DRAFT_API}/draft/league/{league_id}/transactions"), names)
    except Exception as exc:
        log.info("draft transactions for league %s skipped: %s", league_id, exc)

    return LeagueSnapshot("draft", league_id, league_name, table, moves[:RECENT_MOVES], scoring)


def fetch_snapshots(
    client: httpx.Client,
    fpl_league_id: int | None,
    draft_league_id: int | None,
) -> tuple[LeagueSnapshot, ...]:
    """Whichever leagues are configured and answering.

    Never raises: one game 500ing must not cost the other's joke, and no league at all
    returns () so the caller falls back to the static lines rather than prompting a
    model with nothing.
    """
    snapshots = []
    for league_id, fetch in ((fpl_league_id, fetch_classic), (draft_league_id, fetch_draft)):
        if not league_id:
            continue
        try:
            snapshots.append(fetch(client, league_id))
        except Exception as exc:
            log.warning("league snapshot via %s failed: %s", fetch.__name__, exc)
    return tuple(snapshots)


def freshest_move_time(snapshots: tuple[LeagueSnapshot, ...]) -> datetime.datetime | None:
    """The most recent move across every snapshot, or None if nobody has moved.

    Used only for logging: it is the one number that says whether the prompt is about
    to describe this week's transfers or last month's.
    """
    stamps = []
    for snapshot in snapshots:
        for move in snapshot.moves:
            if not move.when:
                continue
            try:
                stamps.append(datetime.datetime.fromisoformat(move.when.replace("Z", "+00:00")))
            except ValueError:
                continue
    return max(stamps, default=None)
