import datetime
import json

from notifier.sources import Moment
from notifier.state import State, load_state, save_state

WHEN = datetime.datetime(2026, 8, 28, 17, 30, tzinfo=datetime.UTC)
MOMENT = Moment("deadline", 2, WHEN, frozenset({"fpl", "draft"}))


def test_a_saved_state_round_trips(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, State(sent={"deadline:2:fpl:24"}, cached={"fpl": [MOMENT]}))
    restored = load_state(path)
    assert restored.sent == {"deadline:2:fpl:24"}
    assert restored.cached == {"fpl": [MOMENT]}


def test_a_restored_moment_keeps_its_utc_awareness(tmp_path):
    """A naive datetime out of the cache would raise TypeError inside due_alerts on the
    first tick after a restart -- exactly when nobody is watching."""
    path = tmp_path / "state.json"
    save_state(path, State(sent=set(), cached={"fpl": [MOMENT]}))
    restored = load_state(path).cached["fpl"][0]
    assert restored.when.tzinfo is not None
    assert restored.when == WHEN


def test_a_restored_moment_keeps_games_as_a_frozenset(tmp_path):
    """JSON has no set type, so this survives a list round trip only if it is rebuilt."""
    path = tmp_path / "state.json"
    save_state(path, State(sent=set(), cached={"fpl": [MOMENT]}))
    assert load_state(path).cached["fpl"][0].games == frozenset({"fpl", "draft"})


def test_a_missing_file_loads_as_empty(tmp_path):
    state = load_state(tmp_path / "absent.json")
    assert state.sent == set()
    assert state.cached == {}


def test_a_corrupt_file_loads_as_empty(tmp_path):
    """Half a JSON object, from a crash mid-write. Refusing to start here would mean a
    truncated file silently costs every future alert."""
    path = tmp_path / "state.json"
    path.write_text('{"sent": ["deadline:2:fpl:24"')
    state = load_state(path)
    assert state.sent == set()


def test_a_file_of_the_wrong_shape_loads_as_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('["not", "an", "object"]')
    assert load_state(path).sent == set()


def test_a_cached_entry_with_an_unreadable_moment_is_dropped_not_fatal(tmp_path):
    """Only that source's cache is lost; the other source and the sent keys survive, so
    a schema change costs a refetch rather than a duplicate alert storm."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "sent": ["deadline:2:fpl:24"],
        "cached": {"fpl": [{"kind": "deadline"}], "draft": []},
    }))
    state = load_state(path)
    assert state.sent == {"deadline:2:fpl:24"}
    assert "fpl" not in state.cached


def test_save_creates_the_parent_directory(tmp_path):
    """data/ is gitignored except for .gitkeep, so a fresh clone on the server can
    plausibly reach the first save without it existing."""
    path = tmp_path / "nested" / "deeper" / "state.json"
    save_state(path, State(sent={"a"}, cached={}))
    assert load_state(path).sent == {"a"}


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    """A crash mid-write must not truncate a good file. Written to a sibling temp and
    renamed, so a reader sees the old file or the new one."""
    path = tmp_path / "state.json"
    save_state(path, State(sent={"a"}, cached={"fpl": [MOMENT]}))
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_overwriting_replaces_rather_than_merges(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, State(sent={"old"}, cached={}))
    save_state(path, State(sent={"new"}, cached={}))
    assert load_state(path).sent == {"new"}


def test_the_file_is_human_readable(tmp_path):
    """It is the only window into why an alert did or did not fire."""
    path = tmp_path / "state.json"
    save_state(path, State(sent={"deadline:2:fpl:24"}, cached={"fpl": [MOMENT]}))
    text = path.read_text()
    assert "deadline:2:fpl:24" in text
    assert "\n" in text
