import datetime
import json
import pathlib

import httpx
import pytest
import respx

from notifier.sources import (
    DRAFT_URL,
    FPL_URL,
    Moment,
    fetch_source,
    merge,
    parse_draft,
    parse_fpl,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
UTC = datetime.UTC


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _at(text):
    return datetime.datetime.fromisoformat(text)


def test_parse_fpl_reads_the_events_list():
    moments = parse_fpl(_fixture("fpl_bootstrap"))
    assert [m.gw for m in moments] == [1, 2, 3, 9]
    assert all(m.kind == "deadline" for m in moments)
    assert all(m.games == frozenset({"fpl"}) for m in moments)


def test_parse_fpl_produces_utc_aware_datetimes():
    """The API sends a trailing Z, which datetime.fromisoformat accepts on 3.13 but
    which produced naive datetimes on older versions. Everything downstream compares
    these against an aware `now`, and mixing the two raises TypeError at the worst
    possible moment."""
    moments = parse_fpl(_fixture("fpl_bootstrap"))
    gw2 = next(m for m in moments if m.gw == 2)
    assert gw2.when == _at("2026-08-28T17:30:00+00:00")
    assert gw2.when.tzinfo is not None
    assert gw2.when.utcoffset() == datetime.timedelta(0)


def test_parse_draft_reads_the_nested_events_data_list():
    """Classic returns `events` as a list; Draft returns a dict with current/next/data.
    Parsing Draft as though it were classic raises TypeError, so this is the one shape
    difference that has to be encoded rather than shared."""
    moments = parse_draft(_fixture("draft_bootstrap"))
    assert {m.gw for m in moments} == {1, 2, 3, 9}


def test_parse_draft_emits_both_a_deadline_and_a_waiver_moment_per_gameweek():
    moments = parse_draft(_fixture("draft_bootstrap"))
    gw2 = [m for m in moments if m.gw == 2]
    kinds = {m.kind: m.when for m in gw2}
    assert kinds == {
        "deadline": _at("2026-08-28T17:30:00+00:00"),
        "waivers": _at("2026-08-27T17:30:00+00:00"),
    }
    assert all(m.games == frozenset({"draft"}) for m in gw2)


def test_parse_draft_ignores_trades_time():
    """Out of scope per the spec. The field is right there in the payload, so this test
    is what stops a future reader adding it because it looked like an oversight."""
    moments = parse_draft(_fixture("draft_bootstrap"))
    assert {m.kind for m in moments} == {"deadline", "waivers"}


def test_parse_skips_an_event_with_a_null_time():
    """Observed on the Draft payload for gameweeks whose waiver window is not yet
    scheduled. A None here would crash fromisoformat during a refresh and take out an
    unrelated pending alert."""
    payload = {"events": {"data": [
        {"id": 5, "deadline_time": "2026-09-18T17:30:00Z", "waivers_time": None},
    ]}}
    moments = parse_draft(payload)
    assert [(m.kind, m.gw) for m in moments] == [("deadline", 5)]


def test_parse_fpl_raises_on_a_missing_events_key():
    """A 200 carrying `{"detail": "maintenance"}` must not parse as "no deadlines" --
    that reading is exactly what would overwrite fetch_source's last-good cache."""
    with pytest.raises(ValueError):
        parse_fpl({"detail": "maintenance"})


def test_parse_fpl_raises_on_an_empty_events_list():
    """An empty list is indistinguishable from "no deadlines" downstream, and FPL
    always publishes 38 gameweeks, so an empty list here is never legitimate."""
    with pytest.raises(ValueError):
        parse_fpl({"events": []})


def test_parse_draft_raises_on_a_missing_data_key():
    """`events` present but without its nested `data` list -- the Draft-shaped analogue
    of the FPL maintenance page."""
    with pytest.raises(ValueError):
        parse_draft({"events": {}})


def test_parse_draft_raises_on_an_empty_data_list():
    with pytest.raises(ValueError):
        parse_draft({"events": {"data": []}})


def test_merge_unions_the_games_of_identical_moments():
    """The live check on 2026-08-22: both games put GW2 at 17:30Z. One merged moment
    means one alert instead of two saying the same thing a second apart."""
    merged = merge(parse_fpl(_fixture("fpl_bootstrap")), parse_draft(_fixture("draft_bootstrap")))
    gw2_deadline = [m for m in merged if m.gw == 2 and m.kind == "deadline"]
    assert len(gw2_deadline) == 1
    assert gw2_deadline[0].games == frozenset({"fpl", "draft"})


def test_merge_keeps_moments_that_differ_by_one_minute_apart():
    """The games have diverged before. An exact-equality merge must not round them
    together, or a genuinely earlier deadline would be announced at the later time."""
    fpl = Moment("deadline", 2, _at("2026-08-28T17:30:00+00:00"), frozenset({"fpl"}))
    draft = Moment("deadline", 2, _at("2026-08-28T17:31:00+00:00"), frozenset({"draft"}))
    merged = merge([fpl], [draft])
    assert len(merged) == 2
    assert [m.games for m in merged] == [frozenset({"fpl"}), frozenset({"draft"})]


def test_merge_keeps_a_waiver_and_a_deadline_at_the_same_instant_separate():
    """Same instant, different kind. Merging on time alone would collapse a waiver
    warning into a deadline warning and lose one of them."""
    a = Moment("deadline", 2, _at("2026-08-28T17:30:00+00:00"), frozenset({"fpl"}))
    b = Moment("waivers", 3, _at("2026-08-28T17:30:00+00:00"), frozenset({"draft"}))
    assert len(merge([a], [b])) == 2


def test_merge_returns_moments_sorted_by_time():
    merged = merge(parse_fpl(_fixture("fpl_bootstrap")), parse_draft(_fixture("draft_bootstrap")))
    assert merged == sorted(merged, key=lambda m: m.when)


def test_merge_of_nothing_is_empty():
    assert merge() == []
    assert merge([], []) == []


@respx.mock
def test_fetch_source_fpl_calls_the_classic_endpoint():
    route = respx.get(FPL_URL).mock(return_value=httpx.Response(200, json=_fixture("fpl_bootstrap")))
    with httpx.Client() as client:
        moments = fetch_source(client, "fpl")
    assert route.called
    assert len(moments) == 4


@respx.mock
def test_fetch_source_draft_calls_the_draft_endpoint():
    route = respx.get(DRAFT_URL).mock(return_value=httpx.Response(200, json=_fixture("draft_bootstrap")))
    with httpx.Client() as client:
        moments = fetch_source(client, "draft")
    assert route.called
    assert len(moments) == 8


@respx.mock
def test_fetch_source_raises_on_a_server_error():
    """The caller's job is to fall back to cache, which it can only do if this raises
    rather than returning an empty list. An empty list would read as "no deadlines
    exist" and silently retire every pending alert."""
    respx.get(FPL_URL).mock(return_value=httpx.Response(503))
    with httpx.Client() as client, pytest.raises(httpx.HTTPError):
        fetch_source(client, "fpl")


@respx.mock
def test_fetch_source_raises_on_a_connection_failure():
    respx.get(FPL_URL).mock(side_effect=httpx.ConnectError("no route"))
    with httpx.Client() as client, pytest.raises(httpx.HTTPError):
        fetch_source(client, "fpl")


def test_fetch_source_rejects_an_unknown_game():
    with httpx.Client() as client, pytest.raises(ValueError):
        fetch_source(client, "nonsense")


def test_moment_is_hashable_and_frozen():
    """State serialisation and the merge both put these in sets."""
    m = Moment("deadline", 1, _at("2026-08-21T17:30:00+00:00"), frozenset({"fpl"}))
    assert {m, m} == {m}
    with pytest.raises(Exception):
        m.gw = 2  # type: ignore[misc]
