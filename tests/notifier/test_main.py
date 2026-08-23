import datetime
import json
import pathlib
import zoneinfo

import httpx
import pytest
import respx

from notifier.__main__ import main, run_once
from notifier.config import Config
from notifier.sources import DRAFT_URL, FPL_URL, Moment
from notifier.state import State, load_state
from notifier.telegram import api_url

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
TOKEN = "123:abc"
SEND_URL = api_url(TOKEN)
OK = {"ok": True, "result": {"message_id": 1}}

DEADLINE = datetime.datetime(2026, 8, 28, 17, 30, tzinfo=datetime.UTC)


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


@respx.mock
def test_a_tick_two_hours_before_the_deadline_sends_one_merged_message(tmp_path):
    _mock_apis()
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    state = State(sent=set(), cached={})
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
    state = State(sent=set(), cached={})
    now = DEADLINE - datetime.timedelta(hours=2)
    with httpx.Client() as client:
        run_once(cfg, client, state, now, refresh=True)
        run_once(cfg, client, state, now + datetime.timedelta(minutes=1), refresh=False)
    assert send.call_count == 1
    # Both games, because GW2's deadline is one merged moment covering the pair.
    assert {"deadline:2:fpl:2", "deadline:2:draft:2"} <= load_state(cfg.state_path).sent


@respx.mock
def test_a_failed_send_leaves_the_key_unwritten_so_the_next_tick_retries(tmp_path, monkeypatch):
    """The single most important behaviour here: a Telegram outage must postpone the
    alert, never consume it."""
    # Without this the two real backoff sleeps cost the suite five seconds.
    monkeypatch.setattr("notifier.telegram.time.sleep", lambda _: None)
    _mock_apis()
    send = respx.post(SEND_URL).mock(side_effect=[
        httpx.Response(500), httpx.Response(500), httpx.Response(500),
        httpx.Response(200, json=OK),
    ])
    cfg = _cfg(tmp_path)
    state = State(sent=set(), cached={})
    now = DEADLINE - datetime.timedelta(hours=2)
    with httpx.Client() as client:
        run_once(cfg, client, state, now, refresh=True)
        # Not `== set()`: this tick also retires the superseded 24h alert and the whole
        # of GW1, and those are written. The point is that the *failed* key is not.
        assert "deadline:2:fpl:2" not in load_state(cfg.state_path).sent
        run_once(cfg, client, state, now + datetime.timedelta(minutes=1), refresh=False)
    assert send.call_count == 4
    assert "deadline:2:fpl:2" in load_state(cfg.state_path).sent


@respx.mock
def test_nothing_due_means_no_telegram_call_at_all(tmp_path):
    _mock_apis()
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    with httpx.Client() as client:
        run_once(_cfg(tmp_path), client, State(sent=set(), cached={}),
                 DEADLINE - datetime.timedelta(days=30), refresh=True)
    assert send.call_count == 0


@respx.mock
def test_a_refresh_caches_both_sources_to_disk(tmp_path):
    _mock_apis()
    cfg = _cfg(tmp_path)
    state = State(sent=set(), cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, DEADLINE - datetime.timedelta(days=30), refresh=True)
    cached = load_state(cfg.state_path).cached
    assert set(cached) == {"fpl", "draft"}
    assert cached["fpl"] and cached["draft"]


@respx.mock
def test_refresh_false_makes_no_api_calls(tmp_path):
    """The loop refetches hourly, not every minute. 48 requests a day, not 2880."""
    fpl = respx.get(FPL_URL).mock(return_value=httpx.Response(200, json=_fixture("fpl_bootstrap")))
    respx.get(DRAFT_URL).mock(return_value=httpx.Response(200, json=_fixture("draft_bootstrap")))
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    state = State(sent=set(), cached={"fpl": [Moment("deadline", 2, DEADLINE, frozenset({"fpl"}))]})
    with httpx.Client() as client:
        run_once(_cfg(tmp_path), client, state, DEADLINE - datetime.timedelta(hours=2), refresh=False)
    assert fpl.call_count == 0


@respx.mock
def test_one_source_failing_still_sends_the_other_from_cache(tmp_path):
    """A Draft outage must not suppress an FPL deadline, and the reverse."""
    respx.get(FPL_URL).mock(return_value=httpx.Response(200, json=_fixture("fpl_bootstrap")))
    respx.get(DRAFT_URL).mock(return_value=httpx.Response(503))
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    state = State(sent=set(), cached={"draft": [Moment("waivers", 2, DEADLINE, frozenset({"draft"}))]})
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
    with httpx.Client() as client:
        run_once(_cfg(tmp_path), client, State(sent=set(), cached={}),
                 DEADLINE - datetime.timedelta(hours=2), refresh=True)


@respx.mock
def test_a_restart_after_a_long_outage_retires_stale_alerts_silently(tmp_path):
    """Down for a week, back up after the deadline. Zero messages, and the keys are
    written so a second restart finds nothing left."""
    _mock_apis()
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    state = State(sent=set(), cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, DEADLINE + datetime.timedelta(hours=1), refresh=True)
    assert send.call_count == 0
    assert "deadline:2:fpl:2" in load_state(cfg.state_path).sent


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
        run_once(_cfg(tmp_path), client, State(sent=set(), cached={}),
                 DEADLINE - datetime.timedelta(hours=2), refresh=True)
    assert send.call_count == 1


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
