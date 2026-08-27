import httpx
import pytest
import respx

from notifier.league import (
    DRAFT_API,
    FPL_API,
    MAX_TRANSFER_FETCHES,
    RECENT_MOVES,
    Move,
    element_names,
    fetch_classic,
    fetch_draft,
    fetch_snapshots,
    freshest_move_time,
    parse_classic_table,
    parse_classic_transfers,
    parse_draft_table,
    parse_draft_transactions,
)

BOOTSTRAP = {
    "elements": [
        {"id": 1, "web_name": "Salah"},
        {"id": 2, "web_name": "Haaland"},
        {"id": 3, "web_name": "Watkins"},
        {"id": 4, "web_name": ""},
    ]
}

CLASSIC_STANDINGS = {
    "league": {"id": 42, "name": "The Office"},
    "standings": {
        "results": [
            {"entry": 11, "player_name": "Nils Berg", "entry_name": "Just Bergs",
             "rank": 1, "event_total": 78, "total": 78},
            {"entry": 22, "player_name": "Rex Marlow", "entry_name": "Rocket FC",
             "rank": 2, "event_total": 61, "total": 61},
        ]
    },
}

DRAFT_DETAILS = {
    "league": {"id": 7, "name": "Draft Dodgers", "scoring": "h"},
    "league_entries": [
        {"id": 100, "entry_id": 11, "entry_name": "Just Bergs",
         "player_first_name": "Nils", "player_last_name": "Berg"},
        {"id": 200, "entry_id": 22, "entry_name": "Rocket FC",
         "player_first_name": "Rex", "player_last_name": "Marlow"},
    ],
    "standings": [
        {"league_entry": 200, "rank": 1, "matches_won": 1, "matches_drawn": 0,
         "matches_lost": 0, "total": 3, "points_for": 55},
        {"league_entry": 100, "rank": 2, "matches_won": 0, "matches_drawn": 0,
         "matches_lost": 1, "total": 0, "points_for": 41},
    ],
}

DRAFT_TRANSACTIONS = {
    "transactions": [
        {"entry": 11, "event": 2, "element_in": 2, "element_out": 3, "kind": "w",
         "result": "d", "added": "2026-08-26T09:00:00Z"},
        {"entry": 22, "event": 2, "element_in": 1, "element_out": 2, "kind": "f",
         "result": "a", "added": "2026-08-27T10:00:00Z"},
    ]
}


# --------------------------------------------------------------------- pure parsers


def test_element_names_maps_ids_to_web_names():
    assert element_names(BOOTSTRAP) == {1: "Salah", 2: "Haaland", 3: "Watkins", 4: ""}


def test_element_names_survives_a_payload_with_no_elements():
    assert element_names({}) == {}
    assert element_names({"elements": "nonsense"}) == {}


def test_parse_classic_table_reads_names_and_points():
    name, table = parse_classic_table(CLASSIC_STANDINGS)
    assert name == "The Office"
    assert [row.manager for row in table] == ["Nils Berg", "Rex Marlow"]
    assert table[0].entry == 11
    assert (table[0].rank, table[0].gw_points, table[0].total_points) == (1, 78, 78)


def test_parse_classic_table_refuses_an_empty_table():
    """An empty `results` is what a maintenance page returned with a 200 looks like.
    Raising sends the caller to the static lines; returning () would instead have it
    prompt a model to be rude about a league of nobody."""
    with pytest.raises(ValueError):
        parse_classic_table({"league": {"name": "x"}, "standings": {"results": []}})


def test_parse_classic_transfers_names_both_ends_of_the_move():
    moves = parse_classic_transfers(
        [{"event": 2, "element_in": 2, "element_out": 1, "time": "2026-08-27T12:00:00Z"}],
        element_names(BOOTSTRAP),
        entry=11,
    )
    assert moves == (
        Move(entry=11, gw=2, player_in="Haaland", player_out="Salah", kind="transfer",
             accepted=True, when="2026-08-27T12:00:00Z"),
    )


def test_parse_classic_transfers_renders_an_unknown_player_as_someone():
    """A player id the bootstrap does not carry (a transfer from a previous season's
    squad) must not put "None" in a joke."""
    moves = parse_classic_transfers(
        [{"event": 1, "element_in": 999, "element_out": 4, "time": None}],
        element_names(BOOTSTRAP),
        entry=11,
    )
    assert (moves[0].player_in, moves[0].player_out) == ("someone", "someone")


def test_parse_classic_transfers_treats_no_transfers_as_normal():
    assert parse_classic_transfers([], {}, 11) == ()
    assert parse_classic_transfers({"detail": "Not found"}, {}, 11) == ()


def test_parse_draft_table_joins_standings_to_entries():
    name, table, links, scoring = parse_draft_table(DRAFT_DETAILS)
    assert name == "Draft Dodgers"
    assert scoring == "h2h"
    assert [row.manager for row in table] == ["Rex Marlow", "Nils Berg"]
    assert table[0].record == "1-0-0"
    assert (table[0].league_points, table[0].points_for) == (3, 55)
    # league_entry -> entry_id, the mapping transactions are keyed by
    assert links == {100: 11, 200: 22}


# A Draft PL league can score like a classic one -- `scoring: "c"`, no matches at all.
# Its standings rows then carry event_total/total and none of the H2H keys, and reading
# `total` as league points would print "53 league pts" for a league that plays no
# fixtures. Observed live on a real classic-scoring league, which is what sent this branch
# into existence.
DRAFT_CLASSIC_DETAILS = {
    "league": {"id": 9, "name": "The Testers", "scoring": "c"},
    "league_entries": [
        {"id": 300, "entry_id": 33, "entry_name": "Harringtons",
         "player_first_name": "Tomas", "player_last_name": "Harrington"},
    ],
    "standings": [{"league_entry": 300, "rank": 1, "event_total": 53, "total": 53}],
    "matches": [],
}


def test_parse_draft_table_reads_a_classic_scoring_league_as_points_not_a_record():
    _, table, _, scoring = parse_draft_table(DRAFT_CLASSIC_DETAILS)
    assert scoring == "classic"
    row = table[0]
    assert (row.gw_points, row.total_points) == (53, 53)
    assert (row.record, row.league_points, row.points_for) == (None, None, None)


def test_parse_draft_table_keeps_h2h_totals_out_of_the_points_columns():
    """`total` is league points in an H2H league and accumulated score in a classic one.
    One key, two meanings."""
    _, table, _, _ = parse_draft_table(DRAFT_DETAILS)
    assert (table[0].gw_points, table[0].total_points) == (None, None)


def test_parse_draft_table_refuses_a_payload_with_no_standings():
    with pytest.raises(ValueError):
        parse_draft_table({"league_entries": [], "standings": []})


@respx.mock
def test_fetch_draft_carries_the_scoring_mode_into_the_snapshot():
    respx.get(f"{DRAFT_API}/league/9/details").mock(
        return_value=httpx.Response(200, json=DRAFT_CLASSIC_DETAILS)
    )
    respx.get(f"{DRAFT_API}/bootstrap-static").mock(return_value=httpx.Response(200, json=BOOTSTRAP))
    respx.get(f"{DRAFT_API}/draft/league/9/transactions").mock(
        return_value=httpx.Response(200, json={"transactions": []})
    )
    with httpx.Client() as client:
        assert fetch_draft(client, 9).scoring == "classic"


def test_parse_draft_transactions_spells_out_kinds_and_keeps_rejections():
    moves = parse_draft_transactions(DRAFT_TRANSACTIONS, element_names(BOOTSTRAP))
    # Newest first: the free-agent pickup was added a day after the failed waiver.
    assert [m.kind for m in moves] == ["free agent", "waiver"]
    rejected = moves[1]
    assert rejected.accepted is False
    assert (rejected.player_in, rejected.player_out) == ("Haaland", "Watkins")


def test_parse_draft_transactions_keeps_an_unknown_kind_verbatim():
    moves = parse_draft_transactions(
        {"transactions": [{"entry": 1, "event": 1, "kind": "z", "result": "a"}]}, {}
    )
    assert moves[0].kind == "z"


def test_freshest_move_time_picks_the_latest_across_snapshots():
    with respx.mock:
        respx.get(f"{DRAFT_API}/league/7/details").mock(return_value=httpx.Response(200, json=DRAFT_DETAILS))
        respx.get(f"{DRAFT_API}/bootstrap-static").mock(return_value=httpx.Response(200, json=BOOTSTRAP))
        respx.get(f"{DRAFT_API}/draft/league/7/transactions").mock(
            return_value=httpx.Response(200, json=DRAFT_TRANSACTIONS)
        )
        with httpx.Client() as client:
            snapshot = fetch_draft(client, 7)
    latest = freshest_move_time((snapshot,))
    assert latest.isoformat() == "2026-08-27T10:00:00+00:00"


def test_freshest_move_time_is_none_when_nobody_has_moved():
    assert freshest_move_time(()) is None


# ------------------------------------------------------------------------- fetching


def _classic_mocks(transfers=None):
    respx.get(f"{FPL_API}/leagues-classic/42/standings/").mock(
        return_value=httpx.Response(200, json=CLASSIC_STANDINGS)
    )
    respx.get(f"{FPL_API}/bootstrap-static/").mock(return_value=httpx.Response(200, json=BOOTSTRAP))
    for entry in (11, 22):
        respx.get(f"{FPL_API}/entry/{entry}/transfers/").mock(
            return_value=httpx.Response(200, json=transfers if transfers is not None else [])
        )


@respx.mock
def test_fetch_classic_returns_table_and_moves_newest_first():
    _classic_mocks([
        {"event": 1, "element_in": 1, "element_out": 3, "time": "2026-08-20T08:00:00Z"},
        {"event": 2, "element_in": 2, "element_out": 1, "time": "2026-08-27T08:00:00Z"},
    ])
    with httpx.Client() as client:
        snapshot = fetch_classic(client, 42)
    assert snapshot.game == "fpl"
    assert snapshot.league_name == "The Office"
    assert len(snapshot.table) == 2
    assert [m.when for m in snapshot.moves] == [
        "2026-08-27T08:00:00Z", "2026-08-27T08:00:00Z",
        "2026-08-20T08:00:00Z", "2026-08-20T08:00:00Z",
    ]


@respx.mock
def test_fetch_classic_survives_one_managers_transfers_failing():
    """One deleted or misbehaving entry costs that manager's moves, not the snapshot."""
    respx.get(f"{FPL_API}/leagues-classic/42/standings/").mock(
        return_value=httpx.Response(200, json=CLASSIC_STANDINGS)
    )
    respx.get(f"{FPL_API}/bootstrap-static/").mock(return_value=httpx.Response(200, json=BOOTSTRAP))
    respx.get(f"{FPL_API}/entry/11/transfers/").mock(return_value=httpx.Response(503))
    respx.get(f"{FPL_API}/entry/22/transfers/").mock(
        return_value=httpx.Response(200, json=[
            {"event": 2, "element_in": 1, "element_out": 2, "time": "2026-08-27T08:00:00Z"}
        ])
    )
    with httpx.Client() as client:
        snapshot = fetch_classic(client, 42)
    assert [m.entry for m in snapshot.moves] == [22]


@respx.mock
def test_fetch_classic_caps_the_moves_it_keeps():
    _classic_mocks([
        {"event": 2, "element_in": 1, "element_out": 2, "time": f"2026-08-2{i % 10}T08:00:00Z"}
        for i in range(20)
    ])
    with httpx.Client() as client:
        snapshot = fetch_classic(client, 42)
    assert len(snapshot.moves) == RECENT_MOVES


@respx.mock
def test_fetch_classic_bounds_how_many_entries_it_asks_about():
    """A pasted overall-leaderboard id must not become hundreds of requests."""
    big = {
        "league": {"id": 42, "name": "Overall"},
        "standings": {"results": [
            {"entry": i, "player_name": f"M{i}", "entry_name": f"T{i}", "rank": i,
             "event_total": 1, "total": 1}
            for i in range(1, 40)
        ]},
    }
    respx.get(f"{FPL_API}/leagues-classic/42/standings/").mock(return_value=httpx.Response(200, json=big))
    respx.get(f"{FPL_API}/bootstrap-static/").mock(return_value=httpx.Response(200, json=BOOTSTRAP))
    route = respx.get(url__regex=rf"{FPL_API}/entry/\d+/transfers/").mock(
        return_value=httpx.Response(200, json=[])
    )
    with httpx.Client() as client:
        fetch_classic(client, 42)
    assert route.call_count == MAX_TRANSFER_FETCHES


@respx.mock
def test_fetch_draft_survives_transactions_being_unavailable():
    respx.get(f"{DRAFT_API}/league/7/details").mock(return_value=httpx.Response(200, json=DRAFT_DETAILS))
    respx.get(f"{DRAFT_API}/bootstrap-static").mock(return_value=httpx.Response(200, json=BOOTSTRAP))
    respx.get(f"{DRAFT_API}/draft/league/7/transactions").mock(return_value=httpx.Response(403))
    with httpx.Client() as client:
        snapshot = fetch_draft(client, 7)
    assert snapshot.moves == ()
    assert len(snapshot.table) == 2


@respx.mock
def test_fetch_snapshots_returns_the_game_that_answered():
    """One game down must not cost the other's joke."""
    respx.get(f"{FPL_API}/leagues-classic/42/standings/").mock(return_value=httpx.Response(500))
    respx.get(f"{DRAFT_API}/league/7/details").mock(return_value=httpx.Response(200, json=DRAFT_DETAILS))
    respx.get(f"{DRAFT_API}/bootstrap-static").mock(return_value=httpx.Response(200, json=BOOTSTRAP))
    respx.get(f"{DRAFT_API}/draft/league/7/transactions").mock(
        return_value=httpx.Response(200, json=DRAFT_TRANSACTIONS)
    )
    with httpx.Client() as client:
        snapshots = fetch_snapshots(client, 42, 7)
    assert [s.game for s in snapshots] == ["draft"]


@respx.mock
def test_fetch_snapshots_asks_for_nothing_when_no_league_is_configured():
    with httpx.Client() as client:
        assert fetch_snapshots(client, None, None) == ()
    assert not respx.calls


@respx.mock
def test_fetch_snapshots_returns_empty_when_both_games_fail():
    respx.get(f"{FPL_API}/leagues-classic/42/standings/").mock(return_value=httpx.Response(500))
    respx.get(f"{DRAFT_API}/league/7/details").mock(side_effect=httpx.ConnectError("down"))
    with httpx.Client() as client:
        assert fetch_snapshots(client, 42, 7) == ()
