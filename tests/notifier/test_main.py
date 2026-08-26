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
from notifier.state import KEEP_PENDING_FOR, State, encode_pending_greeting, load_state, save_state
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
def test_a_failed_greeting_is_not_lost_when_the_offset_has_already_moved_on(tmp_path, monkeypatch):
    """Regression test for a real bug: `_greet` used to advance `state.update_offset`
    to `highest + 1` *before* attempting the send. Telegram treats a later offset as
    confirmation and drops the batch -- it never redelivers an update once a higher
    offset has been acknowledged -- so a chat whose send failed was silently gone for
    good, even though nothing was ever recorded in `greeted` to explain why.

    The getUpdates fake here honours the `offset` query parameter the way Telegram
    really does: once tick 1 has (correctly) advanced the offset past update_id 1,
    tick 2's poll returns nothing new. This only passes if retrying a pending
    greeting is independent of the offset -- i.e. if `state.pending_greetings` (not
    another poll) is what tick 2 retries from.

    Tick 1 does not refresh: the *first* attempt at a brand-new add always happens
    regardless of `refresh`, which is what keeps discovery latency low. Tick 2 does
    refresh, since a *retry* of an already-pending chat only happens on a refreshing
    tick -- see finding A part 3. Both are deliberate, not incidental to this test.
    """
    monkeypatch.setattr(
        "notifier.__main__.send_message",
        functools.partial(real_send_message, sleep=lambda _: None),
    )
    added_update = _added_update(1, -100999)

    def handler(request):
        if request.url.params.get("offset") is None:
            return httpx.Response(200, json={"ok": True, "result": [added_update]})
        return httpx.Response(200, json={"ok": True, "result": []})

    respx.get(UPDATES_URL).mock(side_effect=handler)
    send = respx.post(SEND_URL).mock(side_effect=[
        httpx.Response(500), httpx.Response(500), httpx.Response(500),  # tick 1: fails
        httpx.Response(200, json=OK),                                   # tick 2: retried
    ])
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=False)  # discovery: not gated
        assert "-100999" not in state.greeted
        assert "-100999" in state.pending_greetings  # the retry record survives tick 1
        run_once(cfg, client, state, NOT_DUE, refresh=True)  # retry: needs refresh=True
    assert "-100999" in state.greeted
    assert "-100999" not in state.pending_greetings
    assert send.call_count == 4


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
            changed = _greet(
                cfg, client, state, moments, DEADLINE - datetime.timedelta(hours=2), True,
            )
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
            changed = _greet(cfg, client, state, [], DEADLINE, True)
    assert changed is True
    assert state.update_offset == 6
    assert state.greeted == set()


@respx.mock
def test_a_permanent_poll_failure_stops_polling_for_the_rest_of_the_process(tmp_path):
    """A bad or revoked token, or a lingering webhook, will not clear on its own. Once
    `_greet` has seen one of these it must not call getUpdates again -- otherwise a
    service meant to run all season logs the same warning every 60 seconds forever."""
    updates = respx.get(UPDATES_URL).mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=False)
        assert state.polling_disabled is True
        run_once(cfg, client, state, NOT_DUE, refresh=False)
    assert updates.call_count == 1  # the second tick never even tried


@respx.mock
def test_an_unexpected_exception_anywhere_in_greet_does_not_escape_run_once(tmp_path, monkeypatch):
    """Finding: only fetch_updates's call used to be isolated. Nothing realistic raises
    out of format_intro or a chat's send today, but `_greet` is called from `main`'s
    `while True`, which has no try of its own -- anything that did escape here would
    end the process and every future deadline alert with it. This breaks a step nobody
    thought needed a try (formatting the intro text) to prove the net around the whole
    body actually catches it, not just around the network call.

    The assertion is simply that `run_once` completes: if the exception escaped,
    pytest would fail this test with `boom`'s traceback instead of reaching the end.
    """
    respx.get(UPDATES_URL).mock(return_value=httpx.Response(200, json={
        "ok": True, "result": [_added_update(1, -100999)],
    }))

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected formatting bug")

    monkeypatch.setattr("notifier.__main__.format_intro", boom)
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=False)  # must not raise
    assert "-100999" not in state.greeted  # the crashed attempt was not recorded either


# --- fix round 2: pending-greeting retry pathology ----------------------------------


def _offset_aware_single_add(chat_id, title=None):
    """A getUpdates handler that reports one add on the first (offsetless) poll and
    nothing on any later one -- the same fake used to catch finding 1, reused here so
    multi-tick tests do not accidentally rediscover the same chat every tick."""
    added_update = _added_update(1, chat_id, title=title)

    def handler(request):
        if request.url.params.get("offset") is None:
            return httpx.Response(200, json={"ok": True, "result": [added_update]})
        return httpx.Response(200, json={"ok": True, "result": []})

    return handler


@respx.mock
def test_a_permanently_rejected_chat_is_dropped_not_retried_forever(tmp_path):
    """NEW FINDING A, part 1. A 403 (bot blocked/kicked) or 400 (bad chat) is not
    going to become deliverable by being retried -- that is exactly the pathology
    `PermanentPollError` was introduced to remove from the poll side, reappearing on
    the send side. Probed before this fix: five ticks, five attempts, the entry still
    sitting in `pending_greetings` at the end -- one request and one log line every
    tick, forever."""
    respx.get(UPDATES_URL).mock(side_effect=_offset_aware_single_add(-100999))
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(
        403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"}
    ))
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=False)
        assert "-100999" not in state.pending_greetings
        assert "-100999" not in state.greeted
        # Two more refreshing ticks -- a retry *would* be attempted here if the entry
        # were still pending. It must not be, so no further requests happen.
        run_once(cfg, client, state, NOT_DUE, refresh=True)
        run_once(cfg, client, state, NOT_DUE, refresh=True)
    assert send.call_count == 1


@respx.mock
def test_a_transient_send_failure_keeps_the_entry_pending(tmp_path, monkeypatch):
    """Contrast with the permanent case above: running out of attempts against a 500
    is not the same as Telegram saying no, and must not drop the chat."""
    monkeypatch.setattr(
        "notifier.__main__.send_message",
        functools.partial(real_send_message, sleep=lambda _: None),
    )
    respx.get(UPDATES_URL).mock(side_effect=_offset_aware_single_add(-100999))
    respx.post(SEND_URL).mock(return_value=httpx.Response(500))
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=False)
    assert "-100999" in state.pending_greetings
    assert "-100999" not in state.greeted


@respx.mock
def test_a_new_add_is_still_greeted_on_a_non_refreshing_tick(tmp_path):
    """NEW FINDING A, part 3, the half that must not regress: discovery latency is the
    whole reason polling runs every tick, so a brand-new add must not wait for the
    hourly refresh the way a retry now does."""
    respx.get(UPDATES_URL).mock(side_effect=_offset_aware_single_add(-100999))
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=False)
    assert send.call_count == 1
    assert "-100999" in state.greeted


@respx.mock
def test_a_pending_retry_is_attempted_only_when_refresh_is_true(tmp_path, monkeypatch):
    """NEW FINDING A, part 3: a chat already waiting from an earlier tick is not
    latency-sensitive the way a fresh discovery is -- the first attempt already
    happened immediately -- so it is only retried on a refreshing tick."""
    monkeypatch.setattr(
        "notifier.__main__.send_message",
        functools.partial(real_send_message, sleep=lambda _: None),
    )
    respx.get(UPDATES_URL).mock(return_value=httpx.Response(200, json={"ok": True, "result": []}))
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    state = State(
        sent={}, cached={},
        pending_greetings={"-100999": encode_pending_greeting(NOT_DUE, "News")},
    )
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=False)
        assert send.call_count == 0
        assert "-100999" in state.pending_greetings
        run_once(cfg, client, state, NOT_DUE, refresh=True)
    assert send.call_count == 1
    assert "-100999" in state.greeted


@respx.mock
def test_a_bad_key_in_pending_greetings_does_not_block_a_good_one(tmp_path):
    """NEW FINDING B. A non-numeric key used to reach `int()` before any send was
    attempted, inside `_greet`'s own broad except -- so one bad row silently disabled
    greeting for every chat, permanently, behind a generic 'failed unexpectedly' log
    line. Probed before the fix: zero sends across two ticks, for a state file holding
    `{"oops": "X", "-100999": "Good"}`. The fix drops the bad row at load and lets the
    good one through."""
    respx.get(UPDATES_URL).mock(return_value=httpx.Response(200, json={"ok": True, "result": []}))
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    save_state(cfg.state_path, State(
        sent={}, cached={},
        pending_greetings={
            "oops": encode_pending_greeting(NOT_DUE, "Bad"),
            "-100999": encode_pending_greeting(NOT_DUE, "Good"),
        },
    ))
    state = load_state(cfg.state_path, NOT_DUE)
    assert "oops" not in state.pending_greetings  # dropped already, at load
    assert "-100999" in state.pending_greetings
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=True)
    assert send.call_count == 1
    assert "-100999" in state.greeted


@respx.mock
def test_a_long_lived_process_ages_out_a_stale_pending_greeting(tmp_path):
    """Round 3's finding, mirroring `test_a_long_lived_process_prunes_across_the_
    season_boundary` above for the identical shape of bug in `pending_greetings`:
    `load_state` prunes it too, but a process that never restarts -- the operating
    mode this service is built for -- would otherwise never re-check a stale entry's
    age. Built in memory, deliberately bypassing `load_state`, since the whole point
    is that startup is not what does the pruning here."""
    _mock_apis()
    cfg = _cfg(tmp_path)
    stale = NOT_DUE - KEEP_PENDING_FOR - datetime.timedelta(seconds=1)
    state = State(
        sent={}, cached={},
        pending_greetings={"-100999": encode_pending_greeting(stale, "Old")},
    )
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=True)
    assert "-100999" not in state.pending_greetings


# --- fix round 4: bot-level vs chat-level permanent failures ------------------------


@respx.mock
def test_a_401_leaves_the_pending_entry_for_retry_after_the_token_is_fixed(tmp_path):
    """NEW FINDING (round 4). 401 is about the *token*, not this chat -- a revoked
    token would 401 on every pending intro in the same tick, and dropping them all as
    chat-permanent would mean none of them is ever greeted again, even after the
    operator fixes the token and restarts: Telegram will not redeliver a
    my_chat_member update once the offset has moved past it. Contrast with the 403
    case above (`test_a_permanently_rejected_chat_is_dropped_not_retried_forever`),
    which genuinely is about the chat and is correctly dropped."""
    respx.get(UPDATES_URL).mock(side_effect=_offset_aware_single_add(-100999))
    send = respx.post(SEND_URL).mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=False)
    assert "-100999" in state.pending_greetings
    assert "-100999" not in state.greeted
    assert send.call_count == 1  # 401 fails fast, no retry loop inside send_message


@respx.mock
def test_a_pending_entry_left_by_a_401_is_greeted_once_sends_start_succeeding(tmp_path):
    """The point of leaving it pending rather than dropping it: once the operator has
    fixed the token, the chat a 401 spared still gets its intro on a later,
    refreshing tick -- without ever being rediscovered through getUpdates, since that
    add event will not come round again."""
    respx.get(UPDATES_URL).mock(side_effect=_offset_aware_single_add(-100999))
    send = respx.post(SEND_URL).mock(side_effect=[
        httpx.Response(401, json={"ok": False, "description": "Unauthorized"}),  # tick 1
        httpx.Response(200, json=OK),                                            # tick 2
    ])
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=False)
        assert "-100999" in state.pending_greetings
        assert "-100999" not in state.greeted
        run_once(cfg, client, state, NOT_DUE, refresh=True)  # retry needs refresh=True
    assert "-100999" in state.greeted
    assert "-100999" not in state.pending_greetings
    assert send.call_count == 2


def test_greeting_logs_the_chat_id_and_title(tmp_path, caplog):
    """Change 1's other half: the chat id used to be spelled out in the intro's own
    text (see test_render.py's test_the_intro_no_longer_mentions_configuration_
    details), which the owner asked removed since a shared group is not where the
    operator reads it. It still has to land somewhere the operator *does* look --
    `docker logs` -- so this pins that a successful greeting logs both the id and the
    title, not just a bare 'greeted chat'."""
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with respx.mock:
        respx.get(UPDATES_URL).mock(return_value=httpx.Response(200, json={
            "ok": True, "result": [_added_update(1, -100999, title="News")],
        }))
        respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
        with httpx.Client() as client, caplog.at_level(logging.INFO, logger="notifier"):
            _greet(cfg, client, state, [], NOT_DUE, True)
    assert "-100999" in caplog.text
    assert "News" in caplog.text


# --- /nextdeadline -------------------------------------------------------------------


def _command_update(update_id, chat_id, text, chat_type="group"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "date": 1735689600,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": 1, "is_bot": False, "first_name": "Someone"},
            "text": text,
        },
    }


def _offset_aware_single_command(chat_id, text):
    """The command counterpart of `_offset_aware_single_add`: reports one command on
    the first (offsetless) poll, nothing on any later one, so a multi-tick test does
    not keep rediscovering -- and re-replying to -- the same message forever."""
    update = _command_update(1, chat_id, text)

    def handler(request):
        if request.url.params.get("offset") is None:
            return httpx.Response(200, json={"ok": True, "result": [update]})
        return httpx.Response(200, json={"ok": True, "result": []})

    return handler


@respx.mock
def test_nextdeadline_replies_to_the_asking_chat_not_the_configured_one(tmp_path):
    """`cfg.chat_id` ("987" in these tests) is where scheduled alerts go; a
    /nextdeadline reply must go to whoever asked instead."""
    _mock_apis()
    respx.get(UPDATES_URL).mock(side_effect=_offset_aware_single_command(-555, "/nextdeadline"))
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=True)
    bodies = [json.loads(c.request.read()) for c in send.calls]
    assert {b["chat_id"] for b in bodies} == {"-555"}


@respx.mock
def test_nextdeadline_with_a_bot_username_suffix_also_triggers(tmp_path):
    """Telegram commonly suffixes group commands with the bot's own username --
    `/nextdeadline@fantasyreminderbot` has to reach the same code path as the bare
    form."""
    _mock_apis()
    respx.get(UPDATES_URL).mock(
        side_effect=_offset_aware_single_command(-555, "/nextdeadline@fantasyreminderbot")
    )
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=True)
    assert send.call_count == 1


@respx.mock
def test_a_lookalike_message_does_not_trigger_a_reply(tmp_path):
    """A message that merely mentions the command must not fire it -- see
    parse_commands' own tests for the pure-function version of this; this is the
    end-to-end confirmation that __main__ never gets the chance to reply to one."""
    _mock_apis()
    respx.get(UPDATES_URL).mock(
        side_effect=_offset_aware_single_command(-555, "please run /nextdeadline later")
    )
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=True)
    assert send.call_count == 0


@respx.mock
def test_nextdeadline_does_not_touch_state_sent_so_the_scheduled_alert_still_fires(tmp_path):
    """The owner's explicit callout, given its own test as asked: /nextdeadline must
    never write to `state.sent` -- only a real, scheduled send may. Proven the strong
    way, not just by inspecting `state.sent` after the command tick: a command is
    answered on a tick well before the real alert is due, then a second tick is run at
    the real trigger time and the scheduled alert is confirmed to still go out. If the
    command path had accidentally recorded the alert's key, this second tick would
    silently send nothing -- `due_alerts` would find the key already spent."""
    _mock_apis()
    respx.get(UPDATES_URL).mock(side_effect=_offset_aware_single_command(-555, "/nextdeadline"))
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, NOT_DUE, refresh=True)
        assert state.sent == {}
        assert send.call_count == 1  # only the /nextdeadline reply, nothing due yet
        run_once(cfg, client, state, DEADLINE - datetime.timedelta(hours=2), refresh=True)
    # The command was only seen once (see _offset_aware_single_command), so this
    # second tick's one new send is the real, scheduled alert.
    assert send.call_count == 2
    body = json.loads(send.calls[-1].request.read())
    assert body["chat_id"] == "987"
    assert body["text"] == "⏰ GW2 deadline in 2 hours\nFPL + Draft · Fri 28 Aug, 18:30 BST"
    assert "deadline:2:fpl:2" in state.sent


@respx.mock
def test_a_failed_nextdeadline_reply_does_not_disturb_the_alert_or_the_offset(tmp_path, monkeypatch):
    """Isolation, the same shape as `test_a_getupdates_outage_does_not_stop_a_deadline_
    alert`: a /nextdeadline reply that fails outright must not take a real, due
    scheduled alert down with it, and must not leave the update queue wedged -- the
    offset still has to advance so the same undeliverable command is not redelivered
    forever."""
    monkeypatch.setattr(
        "notifier.__main__.send_message",
        functools.partial(real_send_message, sleep=lambda _: None),
    )
    respx.get(FPL_URL).mock(return_value=httpx.Response(200, json=_fixture("fpl_bootstrap")))
    respx.get(DRAFT_URL).mock(return_value=httpx.Response(200, json=_fixture("draft_bootstrap")))
    respx.get(UPDATES_URL).mock(side_effect=_offset_aware_single_command(-555, "/nextdeadline"))
    send = respx.post(SEND_URL).mock(side_effect=[
        httpx.Response(200, json=OK),  # the real alert, sent first (see run_once)
        httpx.Response(500), httpx.Response(500), httpx.Response(500),  # the reply, exhausted
    ])
    cfg = _cfg(tmp_path)
    state = State(sent={}, cached={})
    now = DEADLINE - datetime.timedelta(hours=2)
    with httpx.Client() as client:
        run_once(cfg, client, state, now, refresh=True)  # must not raise
    assert send.call_count == 4
    assert "deadline:2:fpl:2" in state.sent  # the alert went through and was recorded
    assert state.update_offset == 2  # advanced despite the reply failing
