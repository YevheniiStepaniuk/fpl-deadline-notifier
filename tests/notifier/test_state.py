import datetime
import json

import pytest

from notifier.sources import Moment
from notifier.state import KEEP_SENT_FOR, State, load_state, save_state

WHEN = datetime.datetime(2026, 8, 28, 17, 30, tzinfo=datetime.UTC)
MOMENT = Moment("deadline", 2, WHEN, frozenset({"fpl", "draft"}))
NOW = datetime.datetime(2026, 8, 24, tzinfo=datetime.UTC)


def test_a_saved_state_round_trips(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, State(sent={"deadline:2:fpl:24": WHEN.isoformat()}, cached={"fpl": [MOMENT]}))
    restored = load_state(path, NOW)
    assert restored.sent == {"deadline:2:fpl:24": WHEN.isoformat()}
    assert restored.cached == {"fpl": [MOMENT]}


def test_a_restored_moment_keeps_its_utc_awareness(tmp_path):
    """A naive datetime out of the cache would raise TypeError inside due_alerts on the
    first tick after a restart -- exactly when nobody is watching."""
    path = tmp_path / "state.json"
    save_state(path, State(sent={}, cached={"fpl": [MOMENT]}))
    restored = load_state(path, NOW).cached["fpl"][0]
    assert restored.when.tzinfo is not None
    assert restored.when == WHEN


def test_a_restored_moment_keeps_games_as_a_frozenset(tmp_path):
    """JSON has no set type, so this survives a list round trip only if it is rebuilt.

    isinstance, not just equality: a plain `set` compares equal to a `frozenset` with
    the same contents, so an equality check alone would not notice `Moment` losing its
    hashability -- and `Moment` goes into sets during the merge.
    """
    path = tmp_path / "state.json"
    save_state(path, State(sent={}, cached={"fpl": [MOMENT]}))
    restored = load_state(path, NOW).cached["fpl"][0]
    assert isinstance(restored.games, frozenset)
    assert restored.games == frozenset({"fpl", "draft"})


def test_a_missing_file_loads_as_empty(tmp_path):
    state = load_state(tmp_path / "absent.json", NOW)
    assert state.sent == {}
    assert state.cached == {}


def test_a_corrupt_file_loads_as_empty(tmp_path):
    """Half a JSON object, from a crash mid-write. Refusing to start here would mean a
    truncated file silently costs every future alert."""
    path = tmp_path / "state.json"
    path.write_text('{"sent": {"deadline:2:fpl:24": "2026-08-28T17:30:00+00:00"')
    state = load_state(path, NOW)
    assert state.sent == {}


def test_a_file_of_the_wrong_shape_loads_as_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('["not", "an", "object"]')
    assert load_state(path, NOW).sent == {}


@pytest.mark.parametrize(
    "payload",
    [
        '{"sent": 5, "cached": {}}',
        '{"sent": {}, "cached": ["not", "a", "dict"]}',
        '{"sent": [], "cached": null}',
        '{"sent": {"ok": 7}, "cached": {"fpl": "not a list"}}',
    ],
)
def test_a_wrong_shaped_field_degrades_instead_of_raising(tmp_path, payload):
    """Valid JSON, wrong shape -- a hand edit, or the residue of a schema change.

    An always-on service that refuses to start has turned a recoverable file into a
    missed deadline, so every one of these must degrade rather than raise.
    """
    path = tmp_path / "state.json"
    path.write_text(payload)
    state = load_state(path, NOW)  # must not raise
    assert isinstance(state.sent, dict)
    assert isinstance(state.cached, dict)


def test_a_legacy_list_valued_sent_degrades_to_empty(tmp_path):
    """A state file written by the previous version has `sent` as a list, not a dict.

    That degrades to empty rather than raising. The one-time cost is that an alert
    already sent for a still-future moment might be sent a second time, which is much
    cheaper than the alternative: a store that, kept as-is, never sends again once
    gameweek ids wrap around for the new season.
    """
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sent": ["deadline:2:fpl:24"], "cached": {}}))
    assert load_state(path, NOW).sent == {}


def test_pruning_drops_a_stamp_older_than_keep_sent_for(tmp_path):
    path = tmp_path / "state.json"
    stale = NOW - KEEP_SENT_FOR - datetime.timedelta(seconds=1)
    path.write_text(json.dumps({"sent": {"deadline:2:fpl:24": stale.isoformat()}, "cached": {}}))
    assert load_state(path, NOW).sent == {}


def test_pruning_keeps_a_recent_stamp(tmp_path):
    path = tmp_path / "state.json"
    recent = NOW - datetime.timedelta(days=1)
    stamp = recent.isoformat()
    path.write_text(json.dumps({"sent": {"deadline:2:fpl:24": stamp}, "cached": {}}))
    assert load_state(path, NOW).sent == {"deadline:2:fpl:24": stamp}


def test_pruning_drops_an_entry_with_a_naive_stamp(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sent": {"deadline:2:fpl:24": "2026-08-20T00:00:00"}, "cached": {}}))
    assert load_state(path, NOW).sent == {}


def test_pruning_drops_an_entry_with_an_unparseable_stamp(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sent": {"deadline:2:fpl:24": "not-a-date"}, "cached": {}}))
    assert load_state(path, NOW).sent == {}


def test_a_cached_entry_with_an_unreadable_moment_is_dropped_not_fatal(tmp_path):
    """Only that source's cache is lost; the other source and the sent keys survive, so
    a schema change costs a refetch rather than a duplicate alert storm."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "sent": {"deadline:2:fpl:24": WHEN.isoformat()},
        "cached": {"fpl": [{"kind": "deadline"}], "draft": []},
    }))
    state = load_state(path, NOW)
    assert state.sent == {"deadline:2:fpl:24": WHEN.isoformat()}
    assert "fpl" not in state.cached


def test_moment_from_json_rejects_a_string_games_field(tmp_path):
    """A hand-edited `"games": "fpl"` would become {"f","p","l"} via frozenset() without
    raising, minting bogus keys like `deadline:2:f:24`."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "sent": {},
        "cached": {"fpl": [{
            "kind": "deadline", "gw": 2, "when": WHEN.isoformat(), "games": "fpl",
        }]},
    }))
    assert "fpl" not in load_state(path, NOW).cached


def test_moment_from_json_rejects_an_empty_games_list(tmp_path):
    """An empty games list yields no keys at all, which reads downstream as "already
    sent" and silences that alert permanently."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "sent": {},
        "cached": {"fpl": [{
            "kind": "deadline", "gw": 2, "when": WHEN.isoformat(), "games": [],
        }]},
    }))
    assert "fpl" not in load_state(path, NOW).cached


def test_save_creates_the_parent_directory(tmp_path):
    """data/ is gitignored except for .gitkeep, so a fresh clone on the server can
    plausibly reach the first save without it existing."""
    path = tmp_path / "nested" / "deeper" / "state.json"
    save_state(path, State(sent={"a": WHEN.isoformat()}, cached={}))
    assert load_state(path, NOW).sent == {"a": WHEN.isoformat()}


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    """A crash mid-write must not truncate a good file. Written to a sibling temp and
    renamed, so a reader sees the old file or the new one."""
    path = tmp_path / "state.json"
    save_state(path, State(sent={"a": WHEN.isoformat()}, cached={"fpl": [MOMENT]}))
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_overwriting_replaces_rather_than_merges(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, State(sent={"old": WHEN.isoformat()}, cached={}))
    save_state(path, State(sent={"new": WHEN.isoformat()}, cached={}))
    assert load_state(path, NOW).sent == {"new": WHEN.isoformat()}


def test_the_file_is_human_readable(tmp_path):
    """It is the only window into why an alert did or did not fire."""
    path = tmp_path / "state.json"
    save_state(path, State(sent={"deadline:2:fpl:24": WHEN.isoformat()}, cached={"fpl": [MOMENT]}))
    text = path.read_text()
    assert "deadline:2:fpl:24" in text
    assert "\n" in text


def test_a_fresh_state_defaults_the_new_fields_so_old_construction_still_works():
    """Every existing call site builds State with just sent= and cached=; every new
    field needs a default or every one of those breaks."""
    state = State(sent={}, cached={})
    assert state.update_offset is None
    assert state.greeted == set()
    assert state.pending_greetings == {}
    assert state.polling_disabled is False


def test_update_offset_and_greeted_round_trip(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, State(sent={}, cached={}, update_offset=42, greeted={"987", "-100123"}))
    restored = load_state(path, NOW)
    assert restored.update_offset == 42
    assert restored.greeted == {"987", "-100123"}


def test_a_missing_file_defaults_the_new_fields_too(tmp_path):
    state = load_state(tmp_path / "absent.json", NOW)
    assert state.update_offset is None
    assert state.greeted == set()


def test_a_non_int_update_offset_degrades_to_none(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sent": {}, "cached": {}, "update_offset": "not-an-int"}))
    assert load_state(path, NOW).update_offset is None


def test_a_boolean_update_offset_degrades_to_none(tmp_path):
    """bool is an int subclass in Python; `true` surviving a hand edit must not become
    offset 1 and silently drop update_id 0."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sent": {}, "cached": {}, "update_offset": True}))
    assert load_state(path, NOW).update_offset is None


def test_a_non_list_greeted_degrades_to_empty_set(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sent": {}, "cached": {}, "greeted": "987"}))
    assert load_state(path, NOW).greeted == set()


def test_non_string_entries_in_greeted_are_dropped(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sent": {}, "cached": {}, "greeted": ["987", 42, None]}))
    assert load_state(path, NOW).greeted == {"987"}


def test_greeted_is_saved_as_a_sorted_list_not_a_set(tmp_path):
    """JSON has no set type, so this has to be rebuilt as one on load -- and a stable
    on-disk order keeps the file's diffs sane across saves."""
    path = tmp_path / "state.json"
    save_state(path, State(sent={}, cached={}, greeted={"987", "-100123", "555"}))
    raw = json.loads(path.read_text())
    assert raw["greeted"] == ["-100123", "555", "987"]


def test_pending_greetings_round_trips(tmp_path):
    """A chat waiting on a retry must survive a restart -- that is the whole point of
    keeping it separate from `greeted` rather than only in memory."""
    path = tmp_path / "state.json"
    save_state(path, State(
        sent={}, cached={}, pending_greetings={"-100999": "News", "555": ""},
    ))
    restored = load_state(path, NOW)
    assert restored.pending_greetings == {"-100999": "News", "555": ""}


def test_a_non_dict_pending_greetings_degrades_to_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sent": {}, "cached": {}, "pending_greetings": ["not", "a", "dict"]}))
    assert load_state(path, NOW).pending_greetings == {}


def test_non_string_values_in_pending_greetings_are_dropped(tmp_path):
    """A hand-edited or schema-drifted title must not sail through -- `format_intro`
    expects `str | None`, the same discipline `parse_added` applies to a raw title."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "sent": {}, "cached": {},
        "pending_greetings": {"-1": "fine", "-2": 42, "-3": None},
    }))
    assert load_state(path, NOW).pending_greetings == {"-1": "fine"}


def test_polling_disabled_does_not_survive_a_save_and_load_round_trip(tmp_path):
    """The behavioural version of the test above: even if a value did leak onto disk
    somehow, loading it back must still come back False, since only a fresh process
    (a restart) is allowed to re-enable polling."""
    path = tmp_path / "state.json"
    save_state(path, State(sent={}, cached={}, polling_disabled=True))
    raw = json.loads(path.read_text())
    assert "polling_disabled" not in raw
    assert load_state(path, NOW).polling_disabled is False
