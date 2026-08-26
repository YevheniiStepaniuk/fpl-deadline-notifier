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
from notifier.state import (
    KEEP_PENDING_FOR,
    KEEP_SENT_FOR,
    State,
    decode_pending_greeting,
    encode_pending_greeting,
    load_state,
    prune_pending_greetings,
    prune_sent,
    save_state,
)
from notifier.telegram import TelegramError, send_message
from notifier.updates import PermanentPollError, fetch_added

log = logging.getLogger("notifier")

GAMES = ("fpl", "draft")

# A permanent TelegramError is not always about the chat it was sent to. 400 (chat
# not found) and 403 (bot blocked or kicked) genuinely are, and giving up on that one
# chat is correct. 401 (bad token) and 404 (unknown bot) are about the *token* --
# every pending chat gets the same status in the same tick, and dropping them all as
# chat-permanent would mean none of them is ever greeted, even after the operator
# fixes the token and restarts: Telegram will not redeliver a my_chat_member update
# once the offset has moved past it. So these two are excluded from the drop
# decision below and left pending instead, to retry once the token is good again.
_BOT_LEVEL_STATUSES = {401, 404}


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
    refresh: bool,
) -> bool:
    """Poll once for chats that just added the bot, and send each a one-time intro.

    The whole body sits behind one broad except, not just the fetch: nothing
    realistic raises out of `format_intro` or a chat's send today, but this is called
    from a `while True` in `main` with no try of its own, and this is still a
    convenience bolted beside the thing that matters. An exception already applied to
    `state` before it escaped stays applied -- an advanced offset, a chat moved into
    `pending_greetings` -- only the save on *this* tick is skipped, and it catches up
    next time something else changes.
    """
    try:
        return _poll_and_greet(cfg, client, state, moments, now, refresh)
    except Exception as exc:
        log.error("the greeting step failed unexpectedly and was skipped: %s", exc)
        return False


def _poll_and_greet(
    cfg: config.Config,
    client: httpx.Client,
    state: State,
    moments: Iterable[Moment],
    now: datetime.datetime,
    refresh: bool,
) -> bool:
    """The actual poll-and-greet work, unwrapped from `_greet`'s safety net.

    Polling runs on every tick rather than only on a refreshing one: `_refresh`'s
    hourly cadence exists to be polite to two APIs this service does not control, but
    getUpdates is Telegram's own endpoint and the whole reason it was chosen over a
    webhook was to stay responsive without a second thread. Gating discovery on the
    hourly refresh would mean an intro arriving up to an hour after someone adds the
    bot.

    Polling and sending are deliberately independent state: the offset only ever
    means "Telegram has confirmed I've seen up to here", and advancing it is safe
    regardless of whether a send later fails. Who still needs greeting lives in
    `state.pending_greetings` instead, added to on the way in and removed only once a
    send actually succeeds -- so a failed send is retried later no matter what the
    offset has moved on to.

    Retrying, unlike discovering, is *not* latency-sensitive: the first attempt at a
    newly-added chat already happens this same tick, on every tick, regardless of
    `refresh`. A chat already in `pending_greetings` from an earlier tick is only
    retried when `refresh` is true. Retried on every tick, a chat that is transiently
    failing (rate limits, a Telegram-side blip) would cost three attempts with 1s/4s
    backoff every 60 seconds, forever, for something nobody is waiting on with the
    same urgency as a brand-new add.
    """
    changed = False
    just_added: set[str] = set()

    if state.polling_disabled:
        # Already given up this process's lifetime -- see `PermanentPollError` and
        # `State.polling_disabled`. Chats already pending still deserve their retry
        # below; only the network poll itself is skipped.
        pass
    else:
        try:
            added, highest = fetch_added(client, cfg.bot_token, state.update_offset)
        except PermanentPollError as exc:
            # Logged once, here, and never again this process: the cause needs a
            # restart to clear, so repeating the warning every tick would just be
            # noise for a service meant to run a whole season.
            log.error("polling for chat updates disabled: %s", exc)
            state.polling_disabled = True
        except Exception as exc:
            log.warning("polling for chat updates failed (%s); will retry next tick", exc)
        else:
            if highest is not None:
                # +1, not `highest` itself: Telegram treats the offset as "give me
                # updates after this id", so sending `highest` back would redeliver it
                # forever. Get this wrong the other way (highest + 2) and an update
                # silently never arrives.
                state.update_offset = highest + 1
                changed = True
            for chat in added:
                key = str(chat.chat_id)
                if key in state.greeted or key in state.pending_greetings:
                    continue
                # Private chats are included on purpose, not an oversight: Telegram
                # sends my_chat_member the moment a user first /start's the bot, and
                # that genuinely is the setup flow -- it is how you learn your own
                # chat id if you want alerts sent to a DM rather than a channel. The
                # `greeted`/`pending_greetings` dedupe above is what keeps this
                # bounded to one message per chat rather than one per /start.
                state.pending_greetings[key] = encode_pending_greeting(now, chat.title or "")
                just_added.add(key)
                changed = True

    next_deadline = _next_deadline(moments, now)
    for key, value in list(state.pending_greetings.items()):
        if key not in just_added and not refresh:
            # A retry, not a fresh discovery -- the latency budget that justifies
            # polling every tick does not apply here. See the docstring above.
            continue

        try:
            chat_id = int(key)
        except ValueError:
            chat_id = None
        decoded = decode_pending_greeting(value)
        if chat_id is None or decoded is None:
            # `load_state` already keeps a malformed row from ever reaching here, but
            # a `State` built directly (as tests do, and as a future caller might)
            # skips that check. Dropping the one bad entry here, rather than letting
            # `int()` or `format_intro`'s annotation raise, is what stops it from
            # taking every *other* pending chat down through `_greet`'s broad except.
            log.error("dropping an unreadable pending greeting for chat %r", key)
            del state.pending_greetings[key]
            changed = True
            continue

        _first_seen, title = decoded
        text = format_intro(chat_id, title or None, next_deadline, cfg.tz)
        try:
            send_message(client, cfg.bot_token, key, text)
        except TelegramError as exc:
            if exc.permanent and exc.status_code not in _BOT_LEVEL_STATUSES:
                # Telegram has said, unambiguously, that *this chat* will not go
                # through -- blocked, kicked, or gone. Retrying is not patience, it
                # is the exact pathology `PermanentPollError` was just introduced to
                # remove from the poll side, reappearing on the send side: one
                # request and one log line every tick, forever, for a chat that will
                # never accept the message.
                log.error("giving up on chat %s: %s", key, exc)
                del state.pending_greetings[key]
                changed = True
            else:
                # Left in `pending_greetings`, so a later tick retries -- the same
                # asymmetry `run_once` already uses for alerts: a chat that never
                # sees the intro is worse than one that sees it twice. Also where a
                # 401/404 lands: those are about the token, not this chat, and
                # dropping the entry would mean it is never greeted even after the
                # token is fixed, since Telegram will not redeliver the add event.
                log.error("intro to chat %s failed, will retry later: %s", key, exc)
            continue
        log.info("greeted chat %s", key)
        state.greeted.add(key)
        del state.pending_greetings[key]
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

        # Same fix as `sent`'s, for the same reason. `load_state` prunes
        # `pending_greetings` too, but a process that never restarts would otherwise
        # never re-check a stale entry's age -- exactly the gap a whole-branch review
        # once found in `sent`'s load-only pruning, on a process that stayed up across
        # the June boundary. Only touched when something is actually dropped, so a
        # tick that prunes nothing does not rewrite `state` for no reason.
        kept_pending = prune_pending_greetings(state.pending_greetings, now)
        if len(kept_pending) != len(state.pending_greetings):
            log.info(
                "forgot %d pending greeting(s) whose first-seen stamp is more than "
                "%s past",
                len(state.pending_greetings) - len(kept_pending),
                KEEP_PENDING_FOR,
            )
            state.pending_greetings = kept_pending

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

    greeted = _greet(cfg, client, state, moments, now, refresh)

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
