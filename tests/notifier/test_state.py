import datetime
import json

import pytest

from notifier.sources import Moment
from notifier.state import (
    KEEP_PENDING_FOR,
    KEEP_SENT_FOR,
    State,
    decode_pending_greeting,
    encode_pending_greeting,
    load_banter_ai_last_at,
    load_banter_state,
    load_state,
    save_state,
)

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


def test_encode_and_decode_pending_greeting_round_trips():
    value = encode_pending_greeting(WHEN, "News")
    assert decode_pending_greeting(value) == (WHEN, "News")


def test_a_title_containing_a_pipe_survives_the_round_trip():
    """`partition("|")` splits on the *first* separator only, so a title that happens
    to contain a pipe of its own is not truncated."""
    value = encode_pending_greeting(WHEN, "Fish | Chips FC")
    assert decode_pending_greeting(value) == (WHEN, "Fish | Chips FC")


def test_decode_rejects_a_value_with_no_separator():
    assert decode_pending_greeting("not-encoded-at-all") is None


def test_decode_rejects_an_unparseable_first_seen_stamp():
    assert decode_pending_greeting("not-a-date|News") is None


def test_decode_rejects_a_naive_first_seen_stamp():
    naive = datetime.datetime(2026, 8, 24).isoformat()  # no tzinfo
    assert decode_pending_greeting(f"{naive}|News") is None


def test_pending_greetings_round_trips(tmp_path):
    """A chat waiting on a retry must survive a restart -- that is the whole point of
    keeping it separate from `greeted` rather than only in memory."""
    path = tmp_path / "state.json"
    encoded = {
        "-100999": encode_pending_greeting(NOW, "News"),
        "555": encode_pending_greeting(NOW, ""),
    }
    save_state(path, State(sent={}, cached={}, pending_greetings=encoded))
    restored = load_state(path, NOW)
    assert restored.pending_greetings == encoded


def test_a_non_dict_pending_greetings_degrades_to_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sent": {}, "cached": {}, "pending_greetings": ["not", "a", "dict"]}))
    assert load_state(path, NOW).pending_greetings == {}


def test_non_string_values_in_pending_greetings_are_dropped(tmp_path):
    """A hand-edited or schema-drifted value must not sail through -- `format_intro`
    expects `str | None`, the same discipline `parse_added` applies to a raw title."""
    path = tmp_path / "state.json"
    good = encode_pending_greeting(NOW, "fine")
    path.write_text(json.dumps({
        "sent": {}, "cached": {},
        "pending_greetings": {"-1": good, "-2": 42, "-3": None},
    }))
    assert load_state(path, NOW).pending_greetings == {"-1": good}


def test_a_pending_greeting_with_an_unparseable_value_is_dropped(tmp_path):
    """No "|" at all -- `decode_pending_greeting` returns None for it, and a value
    that can't be decoded can't be retried from either, so it is dropped exactly like
    a `sent` entry with an unparseable stamp."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "sent": {}, "cached": {}, "pending_greetings": {"-1": "no-separator-here"},
    }))
    assert load_state(path, NOW).pending_greetings == {}


def test_a_non_numeric_key_in_pending_greetings_is_dropped(tmp_path):
    """`_greet` calls `int(key)` to build the chat id `format_intro` needs, and that
    used to happen before any send was attempted. A non-numeric key surviving load
    would raise there and, because that call sits inside `_greet`'s own broad except,
    take every *other* pending chat down with it for the tick -- silently, behind a
    generic "failed unexpectedly" line. This is finding B: the fix is here, at load,
    not at the point of failure."""
    path = tmp_path / "state.json"
    good = encode_pending_greeting(NOW, "Good")
    path.write_text(json.dumps({
        "sent": {}, "cached": {},
        "pending_greetings": {"oops": encode_pending_greeting(NOW, "Bad"), "-100999": good},
    }))
    assert load_state(path, NOW).pending_greetings == {"-100999": good}


def test_a_pending_greeting_older_than_keep_pending_for_is_dropped(tmp_path):
    """An intro this late has lost its purpose, and is also what stops a chat that
    fails every send from sitting here forever in a process that never restarts to
    re-check its age."""
    path = tmp_path / "state.json"
    stale = NOW - KEEP_PENDING_FOR - datetime.timedelta(seconds=1)
    path.write_text(json.dumps({
        "sent": {}, "cached": {},
        "pending_greetings": {"-1": encode_pending_greeting(stale, "Old")},
    }))
    assert load_state(path, NOW).pending_greetings == {}


def test_a_fresh_pending_greeting_survives_load(tmp_path):
    path = tmp_path / "state.json"
    recent = NOW - datetime.timedelta(hours=1)
    value = encode_pending_greeting(recent, "Fresh")
    path.write_text(json.dumps({
        "sent": {}, "cached": {}, "pending_greetings": {"-1": value},
    }))
    assert load_state(path, NOW).pending_greetings == {"-1": value}


def test_a_fresh_state_defaults_the_banter_fields_too():
    """Every existing State() call site (this file's own MOMENT-based ones included)
    builds without banter_used/banter_last_target; both need a default or every one
    of those breaks, matching test_a_fresh_state_defaults_the_new_fields_..._above."""
    state = State(sent={}, cached={})
    assert state.banter_used == frozenset()
    assert state.banter_last_target is None


def test_banter_state_round_trips(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, State(
        sent={}, cached={}, banter_used=frozenset({"3", "7"}), banter_last_target="@bob_fpl",
    ))
    restored = load_state(path, NOW)
    assert restored.banter_used == frozenset({"3", "7"})
    assert restored.banter_last_target == "@bob_fpl"


def test_banter_used_is_saved_as_a_sorted_list_not_a_set(tmp_path):
    """JSON has no set type, and a stable on-disk order keeps the file's diffs sane
    across saves -- the same reason `greeted` is sorted before it is written."""
    path = tmp_path / "state.json"
    save_state(path, State(sent={}, cached={}, banter_used=frozenset({"20", "3", "7"})))
    raw = json.loads(path.read_text())
    assert raw["banter_used"] == ["20", "3", "7"]


def test_load_banter_state_round_trips_a_good_pair():
    used, target = load_banter_state(["3", "7"], "@bob_fpl")
    assert used == frozenset({"3", "7"})
    assert target == "@bob_fpl"


def test_load_banter_state_round_trips_no_target():
    used, target = load_banter_state(["1"], None)
    assert used == frozenset({"1"})
    assert target is None


@pytest.mark.parametrize(
    "raw_used, raw_target",
    [
        ("not-a-list", None),  # wrong shape entirely
        ([1, 2, 3], None),  # non-string entries
        (["1"], 42),  # non-string, non-None target
        (["1", None], "@a"),  # a bad entry mixed with a good one
    ],
)
def test_load_banter_state_degrades_a_corrupt_pair_to_a_fresh_cycle(raw_used, raw_target):
    """The spec's explicit requirement: a corrupt banter state must never disable
    the feature, only cost it the current cycle's progress -- the same 'degrade by
    omission, never raise' discipline every other field in this module follows."""
    used, target = load_banter_state(raw_used, raw_target)
    assert used == frozenset()
    assert target is None


def test_a_corrupt_banter_state_on_disk_loads_as_a_fresh_cycle(tmp_path):
    """The behavioural version of the pure-function test above: a hand-edited or
    schema-drifted state.json must not disable banter, only reset its cycle."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "sent": {}, "cached": {},
        "banter_used": "not-a-list", "banter_last_target": 42,
    }))
    state = load_state(path, NOW)
    assert state.banter_used == frozenset()
    assert state.banter_last_target is None


def test_polling_disabled_does_not_survive_a_save_and_load_round_trip(tmp_path):
    """The behavioural version of the test above: even if a value did leak onto disk
    somehow, loading it back must still come back False, since only a fresh process
    (a restart) is allowed to re-enable polling."""
    path = tmp_path / "state.json"
    save_state(path, State(sent={}, cached={}, polling_disabled=True))
    raw = json.loads(path.read_text())
    assert "polling_disabled" not in raw
    assert load_state(path, NOW).polling_disabled is False


# ------------------------------------------------- the AI banter's cooldown timestamp


def test_a_fresh_state_has_no_ai_banter_stamp():
    assert State(sent={}, cached={}).banter_ai_last_at is None


def test_the_ai_banter_stamp_round_trips(tmp_path):
    """Persisted because the cooldown it feeds bounds spend on a paid API, and a deploy
    restarts the process on every push."""
    path = tmp_path / "state.json"
    save_state(path, State(sent={}, cached={}, banter_ai_last_at=NOW))
    assert load_state(path, NOW).banter_ai_last_at == NOW


def test_the_ai_banter_stamp_is_written_as_a_string_not_an_object(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, State(sent={}, cached={}, banter_ai_last_at=NOW))
    assert json.loads(path.read_text())["banter_ai_last_at"] == NOW.isoformat()


@pytest.mark.parametrize("raw", [None, 1735689600, "not a date", "", {"at": "x"}])
def test_an_unusable_ai_banter_stamp_loads_as_none(raw):
    """None permits a call immediately, which is the right direction to fail: the
    alternative would let a corrupt file switch the feature off silently."""
    assert load_banter_ai_last_at(raw) is None


def test_a_naive_ai_banter_stamp_is_assumed_to_be_utc():
    """Everything in this codebase compares against an aware `now`; a naive stamp from
    a hand-edited file would otherwise raise from inside ai_allowed."""
    loaded = load_banter_ai_last_at("2026-08-27T18:00:00")
    assert loaded == datetime.datetime(2026, 8, 27, 18, 0, tzinfo=datetime.UTC)


def test_a_corrupt_ai_banter_stamp_does_not_cost_the_rest_of_the_state(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sent": {}, "cached": {}, "banter_ai_last_at": 42,
                                "banter_used": ["7"], "banter_last_target": "@someone"}))
    state = load_state(path, NOW)
    assert state.banter_ai_last_at is None
    assert state.banter_last_target == "@someone"
