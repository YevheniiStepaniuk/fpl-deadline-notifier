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
from collections.abc import Sequence

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
from notifier.render import format_message
from notifier.schedule import due_alerts
from notifier.state import State, load_state, save_state
from notifier.telegram import TelegramError, send_message

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


def _refresh(client: httpx.Client, state: State) -> None:
    """Refetch each source, keeping the previous copy of anything that fails.

    Per source rather than all-or-nothing: a Draft outage must not suppress an FPL
    deadline. A source that fails with nothing cached simply contributes nothing, which
    is correct -- there is no such thing as a missed alert for a deadline never seen.
    """
    for game in GAMES:
        try:
            state.cached[game] = sources.fetch_source(client, game)
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

    moments = sources.merge(*state.cached.values())
    to_send, to_retire = due_alerts(moments, state.sent, now)

    for alert in to_retire:
        log.info("retiring %s", ", ".join(sorted(alert.keys)))
        # Retirements are recorded whether or not a send follows, so a restart after a
        # long outage settles in one tick instead of re-evaluating every time.
        state.sent.update(alert.keys)

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
            for alert in to_send:
                state.sent.update(alert.keys)

    if to_send or to_retire or refresh:
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

    state = load_state(cfg.state_path)
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
