"""The tick loop.

Wakes on a fixed interval, refetches on a slower one, and asks the pure scheduler what
is owed. Everything decidable lives in schedule.py and render.py; this module only
wires the impure edges together, which is why it has one test per wiring decision
rather than a test per rule.
"""

import argparse
import datetime
import logging
import signal
import sys
import time
from collections.abc import Iterable, Sequence

import httpx

# `config` is imported as a module, not unpacked with `from ... import`, and the
# difference is load-bearing. tests/notifier/test_config.py reloads notifier.config to
# prove the module reads the environment when called rather than at import. A reload
# rebinds every name in that module to *new* objects, including the ConfigError class --
# so a copy of ConfigError taken at import time stops matching the exception
# load_config actually raises, and `except ConfigError` silently stops catching. It
# fails only when both test modules run together, which is the worst way to find out.
# Resolving through the module object looks the name up at call time instead.
from notifier import config, sources
from notifier.render import format_intro, format_message
from notifier.schedule import Alert, due_alerts
from notifier.sources import Moment
from notifier.state import KEEP_SENT_FOR, State, load_state, prune_sent, save_state
from notifier.telegram import TelegramError, send_message
from notifier.updates import fetch_added

log = logging.getLogger("notifier")

GAMES = ("fpl", "draft")


def _should_refresh(
    last_refresh: datetime.datetime | None,
    now: datetime.datetime,
    refresh_seconds: float,
) -> bool:
    """Whether this tick refetches. Pure, so the cadence is testable without a loop.

    Inline in `main` this was the one branch no test could reach, and a regression to
    "every tick" would have gone unnoticed at 2880 requests a day instead of 48.
    """
    return last_refresh is None or (now - last_refresh).total_seconds() >= refresh_seconds


def _log_reschedules(game: str, before: list[Moment], after: list[Moment]) -> None:
    """Report a moment whose instant has changed since the last refresh.

    Alerts are keyed by gameweek, so a moved deadline deliberately does not re-send --
    which means nothing else would ever mention that it moved.
    """
    was = {(m.kind, m.gw): m.when for m in before}
    for moment in after:
        previous = was.get((moment.kind, moment.gw))
        if previous is not None and previous != moment.when:
            log.info(
                "%s %s GW%d moved: %s -> %s",
                game, moment.kind, moment.gw, previous.isoformat(), moment.when.isoformat(),
            )


def _refresh(client: httpx.Client, state: State) -> None:
    """Refetch each source, keeping the previous copy of anything that fails.

    Per source rather than all-or-nothing: a Draft outage must not suppress an FPL
    deadline. A source that fails with nothing cached simply contributes nothing, which
    is correct -- there is no such thing as a missed alert for a deadline never seen.
    """
    for game in GAMES:
        try:
            fetched = sources.fetch_source(client, game)
        except Exception as exc:
            # Deliberately broad. `fetch_source` documents httpx.HTTPError, but it also
            # parses: a 200 maintenance page raises JSONDecodeError, a malformed
            # deadline_time raises ValueError out of fromisoformat, a renamed field
            # raises KeyError. None of those is an HTTPError, so a narrow clause here
            # lets them past `run_once` and out of the `while True` in main -- and then
            # an FPL outage takes down the Draft alert that has nothing to do with it,
            # which is the exact coupling this function exists to prevent.
            held = len(state.cached.get(game, []))
            log.warning("refresh failed for %s (%s); holding %d cached moments", game, exc, held)
        else:
            _log_reschedules(game, state.cached.get(game, []), fetched)
            state.cached[game] = fetched


def _record(state: State, alerts: Iterable[Alert]) -> None:
    """Mark every game's key for these alerts, stamped with the moment's instant.

    The stamp is what `load_state` prunes on. Gameweek ids restart at 1 each season, so
    an unpruned store would let this season's keys suppress all of next season's, with
    no message and nothing in the log to notice it by.
    """
    for alert in alerts:
        stamp = alert.moment.when.isoformat()
        for key in alert.keys:
            state.sent[key] = stamp


def _next_deadline(moments: Iterable[Moment], now: datetime.datetime) -> Moment | None:
    upcoming = [m for m in moments if m.kind == "deadline" and m.when > now]
    return min(upcoming, key=lambda m: m.when, default=None)


def _greet(
    cfg: config.Config,
    client: httpx.Client,
    state: State,
    moments: Iterable[Moment],
    now: datetime.datetime,
) -> bool:
    """Poll once for chats that just added the bot, and send each a one-time intro.

    Runs on every tick rather than only on a refreshing one: `_refresh`'s hourly
    cadence exists to be polite to two APIs this service does not control, but
    getUpdates is Telegram's own endpoint and the whole reason it was chosen over a
    webhook was to stay responsive without a second thread. Gating it on the hourly
    refresh would mean an intro arriving up to an hour after someone adds the bot.

    Only fetch_added's network call is isolated here, the same shape as `_refresh`'s
    per-source try/except: this is a convenience bolted beside the thing that
    matters, and a webhook conflict, a network blip or a malformed update must never
    stop a deadline alert on the same tick. A per-chat send failure is handled
    separately below, for the same reason `run_once` does not let one failed alert
    stop another.
    """
    try:
        added, highest = fetch_added(client, cfg.bot_token, state.update_offset)
    except Exception as exc:
        log.warning("polling for chat updates failed (%s); will retry next tick", exc)
        return False

    changed = False
    if highest is not None:
        # +1, not `highest` itself: Telegram treats the offset as "give me updates
        # after this id", so sending `highest` back would redeliver it forever. Get
        # this wrong the other way (highest + 2) and an update silently never arrives.
        state.update_offset = highest + 1
        changed = True

    if not added:
        return changed

    next_deadline = _next_deadline(moments, now)
    for chat in added:
        key = str(chat.chat_id)
        if key in state.greeted:
            continue
        text = format_intro(chat.chat_id, chat.title, next_deadline, cfg.tz)
        try:
            send_message(client, cfg.bot_token, key, text)
        except TelegramError as exc:
            # Not recorded, so the next tick retries -- the same asymmetry `run_once`
            # already uses for alerts: a chat that never sees the intro is worse than
            # one that sees it twice.
            log.error("intro to chat %s failed, will retry next tick: %s", key, exc)
            continue
        log.info("greeted new chat %s (%s)", key, chat.chat_type)
        state.greeted.add(key)
        changed = True
    return changed


def run_once(
    cfg: config.Config,
    client: httpx.Client,
    state: State,
    now: datetime.datetime,
    refresh: bool,
) -> None:
    """One tick: maybe refetch, work out what is owed, send it, record it."""
    if refresh:
        _refresh(client, state)
        # Pruning belongs to the tick, not only to startup. `load_state` prunes what it
        # reads, but this is a service meant to still be running next year: a process
        # that stays up across the June season boundary would otherwise keep last
        # season's keys forever, and gameweek ids restart at 1 -- so the new season's
        # GW1 alert would be suppressed with no message and nothing in the log. Hourly,
        # alongside the refetch, is far more often than a 30-day window needs.
        kept = prune_sent(state.sent, now)
        if len(kept) != len(state.sent):
            log.info(
                "forgot %d sent key(s) whose moment is more than %s past",
                len(state.sent) - len(kept),
                KEEP_SENT_FOR,
            )
        state.sent = kept

    moments = sources.merge(*state.cached.values())
    to_send, to_retire = due_alerts(moments, state.sent, now)

    for alert in to_retire:
        log.info("retiring %s", ", ".join(sorted(alert.keys)))
    # Retirements are recorded whether or not a send follows, so a restart after a
    # long outage settles in one tick instead of re-evaluating every time.
    _record(state, to_retire)

    if to_send:
        try:
            send_message(client, cfg.bot_token, cfg.chat_id, format_message(to_send, cfg.tz))
        except TelegramError as exc:
            # Deliberately not recorded. The next tick retries, and keeps retrying
            # until it succeeds or the moment passes and retirement takes over.
            log.error("send failed, will retry next tick: %s", exc)
        else:
            log.info("sent %s", ", ".join(sorted(k for a in to_send for k in a.keys)))
            # Every game the alert covered, so a later divergence between the two does
            # not re-fire the half whose key was never written.
            _record(state, to_send)

    greeted = _greet(cfg, client, state, moments, now)

    if to_send or to_retire or refresh or greeted:
        try:
            save_state(cfg.state_path, state)
        except OSError as exc:
            # A full disk or a permission change on data/ must not take down a service
            # whose whole job is to still be running next Friday. The in-memory state
            # is still correct, so nothing is lost until the process restarts.
            log.error("could not write %s: %s", cfg.state_path, exc)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="notifier", description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one tick and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    try:
        cfg = config.load_config()
    except config.ConfigError as exc:
        # Loud and named. A notifier that starts and silently never sends is worse than
        # one that refuses to start.
        print(f"notifier: {exc}", file=sys.stderr)
        return 1

    state = load_state(cfg.state_path, datetime.datetime.now(datetime.UTC))
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        log.info("signal %s received, stopping after this tick", signum)

    if not args.once:
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

    with httpx.Client() as client:
        last_refresh = None
        while True:
            now = datetime.datetime.now(datetime.UTC)
            due = _should_refresh(last_refresh, now, cfg.refresh_seconds)
            run_once(cfg, client, state, now, refresh=due)
            if due:
                last_refresh = now
            if args.once or stopping:
                return 0
            time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
