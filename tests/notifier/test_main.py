import datetime
import functools
import json
import logging
import pathlib
import zoneinfo

import httpx
import pytest
import respx

from notifier.__main__ import main, run_once, _greet, _log_reschedules, _should_refresh
from notifier.config import Config
from notifier.sources import DRAFT_URL, FPL_URL, Moment
from notifier.state import State, load_state, save_state
from notifier.telegram import api_url, send_message as real_send_message

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
TOKEN = "123:abc"
SEND_URL = api_url(TOKEN)
UPDATES_URL = api_url(TOKEN, "getUpdates")
OK = {"ok": True, "result": {"message_id": 1}}

DEADLINE = datetime.datetime(2026, 8, 28, 17, 30, tzinfo=datetime.UTC)


def _added_update(update_id, chat_id, chat_type="channel", title=None):
    chat = {"id": chat_id, "type": chat_type}
    if title is not None:
        chat["title"] = title
    return {
        "update_id": update_id,
        "my_chat_member": {
            "chat": chat,
            "date": 1735689600,
            "from": {"id": 1, "is_bot": False, "first_name": "Someone"},
            "old_chat_member": {"user": {"id": 999, "is_bot": True}, "status": "left"},
            "new_chat_member": {"user": {"id": 999, "is_bot": True}, "status": "member"},
        },
    }


def _no_updates():
    return respx.get(UPDATES_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []})
    )


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _cfg(tmp_path):
    return Config(
        bot_token=TOKEN,
        chat_id="987",
        tz=zoneinfo.ZoneInfo("Europe/London"),
        poll_seconds=60.0,
        refresh_seconds=3600.0,
        state_path=tmp_path / "state.json",
    )


def _mock_apis():
    respx.get(FPL_URL).mock(return_value=httpx.Response(200, json=_fixture("fpl_bootstrap")))
    respx.get(DRAFT_URL).mock(return_value=httpx.Response(200, json=_fixture("draft_bootstrap")))
    # Every existing test in this file predates _greet. Mocking an empty result here,
    # rather than leaving getUpdates unmocked, keeps those tests exercising a clean
    # "nothing to greet" path instead of incidentally relying on respx's own
    # unmocked-route error being swallowed by _greet's isolation.
    _no_updates()


@respx.mock
def test_a_tick_two_hours_before_the_deadline_sends_one_merged_message(tmp_path):
    _mock_apis()
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(_cfg(tmp_path), client, state, DEADLINE - datetime.timedelta(hours=2), refresh=True)
    assert send.call_count == 1
    body = json.loads(send.calls[0].request.read())
    assert body["text"] == "⏰ GW2 deadline in 2 hours\nFPL + Draft · Fri 28 Aug, 18:30 BST"


@respx.mock
def test_a_successful_send_is_persisted_so_the_next_tick_is_silent(tmp_path):
    _mock_apis()
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    now = DEADLINE - datetime.timedelta(hours=2)
    with httpx.Client() as client:
        run_once(cfg, client, state, now, refresh=True)
        run_once(cfg, client, state, now + datetime.timedelta(minutes=1), refresh=False)
    assert send.call_count == 1
    # Both games, because GW2's deadline is one merged moment covering the pair.
    assert {"deadline:2:fpl:2", "deadline:2:draft:2"} <= load_state(cfg.state_path, DEADLINE).sent.keys()


@respx.mock
def test_a_failed_send_leaves_the_key_unwritten_so_the_next_tick_retries(tmp_path, monkeypatch):
    """The single most important behaviour here: a Telegram outage must postpone the
    alert, never consume it."""
    # `send_message`'s `sleep=time.sleep` default is bound at def time, so patching
    # `notifier.telegram.time.sleep` does not reach it -- the earlier attempt at this
    # was a no-op and the test really did cost five seconds. Injecting the fake through
    # a partial is what actually skips the backoff.
    monkeypatch.setattr(
        "notifier.__main__.send_message",
        functools.partial(real_send_message, sleep=lambda _: None),
    )
    _mock_apis()
    send = respx.post(SEND_URL).mock(side_effect=[
        httpx.Response(500), httpx.Response(500), httpx.Response(500),
        httpx.Response(200, json=OK),
    ])
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    now = DEADLINE - datetime.timedelta(hours=2)
    with httpx.Client() as client:
        run_once(cfg, client, state, now, refresh=True)
        # Not `== set()`: this tick also retires the superseded 24h alert and the whole
        # of GW1, and those are written. The point is that the *failed* key is not.
        assert "deadline:2:fpl:2" not in load_state(cfg.state_path, DEADLINE).sent
        run_once(cfg, client, state, now + datetime.timedelta(minutes=1), refresh=False)
    assert send.call_count == 4
    assert "deadline:2:fpl:2" in load_state(cfg.state_path, DEADLINE).sent


@respx.mock
def test_nothing_due_means_no_telegram_call_at_all(tmp_path):
    _mock_apis()
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    with httpx.Client() as client:
        run_once(_cfg(tmp_path), client, State(sent={}, cached={}),
                 DEADLINE - datetime.timedelta(days=30), refresh=True)
    assert send.call_count == 0


@respx.mock
def test_a_refresh_caches_both_sources_to_disk(tmp_path):
    _mock_apis()
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, DEADLINE - datetime.timedelta(days=30), refresh=True)
    cached = load_state(cfg.state_path, DEADLINE).cached
    assert set(cached) == {"fpl", "draft"}
    assert cached["fpl"] and cached["draft"]


@respx.mock
def test_refresh_false_makes_no_api_calls(tmp_path):
    """The loop refetches hourly, not every minute. 48 requests a day, not 2880."""
    fpl = respx.get(FPL_URL).mock(return_value=httpx.Response(200, json=_fixture("fpl_bootstrap")))
    respx.get(DRAFT_URL).mock(return_value=httpx.Response(200, json=_fixture("draft_bootstrap")))
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    _no_updates()
    state = State(sent={}, cached={"fpl": [Moment("deadline", 2, DEADLINE, frozenset({"fpl"}))]})
    with httpx.Client() as client:
        run_once(_cfg(tmp_path), client, state, DEADLINE - datetime.timedelta(hours=2), refresh=False)
    assert fpl.call_count == 0


@respx.mock
def test_one_source_failing_still_sends_the_other_from_cache(tmp_path):
    """A Draft outage must not suppress an FPL deadline, and the reverse."""
    respx.get(FPL_URL).mock(return_value=httpx.Response(200, json=_fixture("fpl_bootstrap")))
    respx.get(DRAFT_URL).mock(return_value=httpx.Response(503))
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    _no_updates()
    state = State(sent={}, cached={"draft": [Moment("waivers", 2, DEADLINE, frozenset({"draft"}))]})
    with httpx.Client() as client:
        run_once(_cfg(tmp_path), client, state, DEADLINE - datetime.timedelta(hours=2), refresh=True)
    text = json.loads(send.calls[0].request.read())["text"]
    # Both halves, asserted separately. An `or` here would pass on either one and hide
    # exactly the failure this test exists to catch.
    assert "GW2 deadline in 2 hours\nFPL · " in text
    assert "GW2 waiver window closes in 2 hours\nDraft · " in text
    assert state.cached["draft"]  # the stale half survived the failed refresh


@respx.mock
def test_both_sources_failing_with_no_cache_is_not_fatal(tmp_path):
    """First run on a box with no network. Nothing to send, nothing lost, no crash."""
    respx.get(FPL_URL).mock(return_value=httpx.Response(503))
    respx.get(DRAFT_URL).mock(side_effect=httpx.ConnectError("no route"))
    _no_updates()
    with httpx.Client() as client:
        run_once(_cfg(tmp_path), client, State(sent={}, cached={}),
                 DEADLINE - datetime.timedelta(hours=2), refresh=True)


@respx.mock
def test_a_restart_after_a_long_outage_retires_stale_alerts_silently(tmp_path):
    """Down for a week, back up after the deadline. Zero messages, and the keys are
    written so a second restart finds nothing left."""
    _mock_apis()
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, DEADLINE + datetime.timedelta(hours=1), refresh=True)
    assert send.call_count == 0
    assert "deadline:2:fpl:2" in load_state(cfg.state_path, DEADLINE).sent


@respx.mock
def test_a_season_rollover_does_not_silence_the_next_seasons_alerts(tmp_path):
    """The defect this fix exists for: FPL's gameweek ids restart at 1 every season, so
    a spent key from last season must not still be sitting there to block this one.

    Without pruning, `load_state` would hand back these keys intact, `due_alerts` would
    find GW2's keys already spent, and the tick below would send nothing at all."""
    _mock_apis()
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    a_year_ago = (DEADLINE - datetime.timedelta(days=365)).isoformat()
    save_state(cfg.state_path, State(
        sent={"deadline:2:fpl:2": a_year_ago, "deadline:2:draft:2": a_year_ago},
        cached={},
    ))
    now = DEADLINE - datetime.timedelta(hours=2)
    state = load_state(cfg.state_path, now)
    assert state.sent == {}
    with httpx.Client() as client:
        run_once(cfg, client, state, now, refresh=True)
    assert send.call_count == 1


@respx.mock
def test_a_long_lived_process_prunes_across_the_season_boundary(tmp_path):
    """The finding this fix closes: `load_state` only prunes what it reads, so a
    process that never restarts -- the operating mode this service is built for --
    would carry last season's keys across the June boundary forever. Built in memory,
    deliberately bypassing `load_state`, since the whole point is that startup is not
    what does the pruning here.

    Both offsets are seeded, not just the 2-hour one: last season's GW2 ran to
    completion, so its 24-hour key was written too. Seeding only the 2-hour key would
    leave the 24-hour alert's key absent and let it fire on its own regardless of
    pruning, which would make this test pass for the wrong reason."""
    _mock_apis()
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    stale = (DEADLINE - datetime.timedelta(days=365)).isoformat()
    state = State(
        sent={
            "deadline:2:fpl:2": stale, "deadline:2:draft:2": stale,
            "deadline:2:fpl:24": stale, "deadline:2:draft:24": stale,
        },
        cached={},
    )
    with httpx.Client() as client:
        run_once(cfg, client, state, DEADLINE - datetime.timedelta(hours=2), refresh=True)
    assert send.call_count == 1


@respx.mock
def test_a_current_sent_key_is_not_pruned_mid_tick(tmp_path):
    """The other half of the same change: pruning on the tick must not turn into a
    duplicate alert for a key whose moment has not gone stale. Both offsets are seeded
    for the same reason as above -- an absent 24-hour key would fire on its own and
    the assertion below would fail regardless of whether pruning behaves correctly."""
    _mock_apis()
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    current = DEADLINE.isoformat()
    state = State(
        sent={
            "deadline:2:fpl:2": current, "deadline:2:draft:2": current,
            "deadline:2:fpl:24": current, "deadline:2:draft:24": current,
        },
        cached={},
    )
    with httpx.Client() as client:
        run_once(cfg, client, state, DEADLINE - datetime.timedelta(hours=2), refresh=True)
    assert send.call_count == 0


def test_a_moved_deadline_is_logged(caplog):
    """The spec's promised log line for a reschedule -- the only visibility into one,
    since alerts are keyed by gameweek and deliberately do not re-send."""
    before = [Moment("deadline", 2, DEADLINE, frozenset({"fpl"}))]
    after = [Moment("deadline", 2, DEADLINE + datetime.timedelta(hours=1), frozenset({"fpl"}))]
    with caplog.at_level(logging.INFO, logger="notifier"):
        _log_reschedules("fpl", before, after)
    assert "moved" in caplog.text


def test_an_unmoved_deadline_is_not_logged(caplog):
    moments = [Moment("deadline", 2, DEADLINE, frozenset({"fpl"}))]
    with caplog.at_level(logging.INFO, logger="notifier"):
        _log_reschedules("fpl", moments, moments)
    assert "moved" not in caplog.text


@respx.mock
def test_an_unwritable_state_file_does_not_kill_the_tick(tmp_path, monkeypatch):
    """A full disk or a permission change on data/ must not take down an always-on
    service. By the time the write happens the alert has already been delivered."""
    _mock_apis()
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))

    def boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr("notifier.__main__.save_state", boom)
    with httpx.Client() as client:
        run_once(_cfg(tmp_path), client, State(sent={}, cached={}),
                 DEADLINE - datetime.timedelta(hours=2), refresh=True)
    assert send.call_count == 1


@pytest.mark.parametrize(
    "elapsed_seconds, expected",
    [
        (None, True),   # first tick of the process: nothing cached yet
        (59, False),    # a minute in, on the default hourly cadence
        (3600, True),   # exactly due -- >=, so this tick refetches
        (7200, True),   # long overdue, e.g. after the machine slept
    ],
)
def test_the_refresh_cadence(elapsed_seconds, expected):
    """48 requests a day, not 2880. Inline in `main` this branch was unreachable by any
    test, so a regression to refetching every tick would have passed the whole suite."""
    now = DEADLINE
    last = None if elapsed_seconds is None else now - datetime.timedelta(seconds=elapsed_seconds)
    assert _should_refresh(last, now, 3600.0) is expected


def test_main_exits_nonzero_with_a_named_variable_when_config_is_missing(monkeypatch, capsys):
    """A notifier that starts and never sends is worse than one that refuses to start."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert main([]) == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


@respx.mock
def test_main_once_runs_a_single_tick_and_exits_zero(monkeypatch, tmp_path):
    """--once is what the systemd unit's ExecStartPre and a manual smoke test use."""
    _mock_apis()
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987")
    monkeypatch.setenv("NOTIFIER_STATE_PATH", str(tmp_path / "state.json"))
    assert main(["--once"]) == 0
    assert (tmp_path / "state.json").exists()


# --- _greet ------------------------------------------------------------------------

NOT_DUE = DEADLINE - datetime.timedelta(days=30)


@respx.mock
def test_a_newly_added_chat_is_greeted_while_alerts_still_go_only_to_cfg_chat_id(tmp_path):
    """The core guarantee of discovery: a channel adding the bot gets the intro, but
    that must never redirect or multiply the deadline alerts themselves."""
    _mock_apis()
    respx.get(UPDATES_URL).mock(return_value=httpx.Response(200, json={
        "ok": True, "result": [_added_update(1, -100999, title="News")],
    }))
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=True)
    bodies = [json.loads(c.request.read()) for c in send.calls]
    assert {b["chat_id"] for b in bodies} == {"-100999"}
    assert state.greeted == {"-100999"}


@respx.mock
def test_a_chat_already_greeted_gets_no_second_intro(tmp_path):
    _mock_apis()
    respx.get(UPDATES_URL).mock(return_value=httpx.Response(200, json={
        "ok": True, "result": [_added_update(1, -100999, title="News")],
    }))
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={}, greeted={"-100999"})
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=True)
    assert send.call_count == 0


@respx.mock
def test_a_failed_greeting_is_not_recorded_so_the_next_tick_retries(tmp_path, monkeypatch):
    """The same asymmetry `run_once` already uses for alerts: an unsent intro must be
    retried, never silently written off as delivered."""
    monkeypatch.setattr(
        "notifier.__main__.send_message",
        functools.partial(real_send_message, sleep=lambda _: None),
    )
    _mock_apis()
    respx.get(UPDATES_URL).mock(return_value=httpx.Response(200, json={
        "ok": True, "result": [_added_update(1, -100999)],
    }))
    respx.post(SEND_URL).mock(return_value=httpx.Response(500))
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=True)
    assert "-100999" not in state.greeted


@respx.mock
def test_updates_are_polled_on_a_non_refreshing_tick(tmp_path):
    """`_refresh` is hourly by design, but getUpdates is Telegram's own endpoint and
    the entire reason short-polling was chosen over a webhook. Gating it on the hourly
    refresh would mean up to an hour of latency for something meant to stay live."""
    updates = respx.get(UPDATES_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []})
    )
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    with httpx.Client() as client:
        run_once(_cfg(tmp_path), client, State(sent={}, cached={}), NOT_DUE, refresh=False)
    assert updates.call_count == 1


@respx.mock
def test_a_getupdates_outage_does_not_stop_a_deadline_alert(tmp_path):
    """Isolation is the point: a Telegram polling failure must never take a real
    deadline alert down with it, exactly as a Draft outage must not suppress FPL's."""
    _mock_apis()
    respx.get(UPDATES_URL).mock(return_value=httpx.Response(401, json={"ok": False}))
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, DEADLINE - datetime.timedelta(hours=2), refresh=True)
    assert send.call_count == 1
    assert json.loads(send.calls[0].request.read())["chat_id"] == "987"


def test_greet_picks_the_earliest_future_deadline_for_the_intro(tmp_path):
    """A past deadline must not be offered as "next", and among several future ones the
    soonest is the one worth naming."""
    cfg = _cfg(tmp_path)
    moments = [
        Moment("deadline", 1, DEADLINE - datetime.timedelta(days=10), frozenset({"fpl"})),
        Moment("deadline", 3, DEADLINE + datetime.timedelta(days=7), frozenset({"fpl"})),
        Moment("deadline", 2, DEADLINE, frozenset({"fpl"})),
    ]
    state = State(sent={}, cached={})
    with respx.mock:
        respx.get(UPDATES_URL).mock(return_value=httpx.Response(200, json={
            "ok": True, "result": [_added_update(1, -100999)],
        }))
        send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
        with httpx.Client() as client:
            changed = _greet(cfg, client, state, moments, DEADLINE - datetime.timedelta(hours=2))
    assert changed is True
    text = json.loads(send.calls[0].request.read())["text"]
    assert "GW2" in text
    assert "GW1" not in text
    assert "GW3" not in text


def test_greet_advances_the_offset_even_with_nothing_to_greet(tmp_path):
    """The offset has to move forward on an empty-but-successful poll too, or the same
    already-seen updates (a promotion, a demotion) would be re-fetched every tick
    forever."""
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with respx.mock:
        respx.get(UPDATES_URL).mock(return_value=httpx.Response(200, json={
            "ok": True, "result": [
                # A promotion: a real my_chat_member update, but not an add.
                {
                    "update_id": 5,
                    "my_chat_member": {
                        "chat": {"id": -1, "type": "channel"},
                        "old_chat_member": {"status": "member"},
                        "new_chat_member": {"status": "administrator"},
                    },
                },
            ],
        }))
        with httpx.Client() as client:
            changed = _greet(cfg, client, state, [], DEADLINE)
    assert changed is True
    assert state.update_offset == 6
    assert state.greeted == set()
