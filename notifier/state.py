"""The notifier's memory: which alerts have been handled, and the last good deadlines.

One file for both, because they are written on the same tick and a half-restored pair
would be worse than either alone.

Every read path degrades to empty rather than raising. A notifier that refuses to start
because of a truncated JSON file has turned a recoverable problem into a missed
deadline, and the schedule's retirement rule already makes an empty state cheap: on the
next tick everything already past is retired silently instead of sent.
"""

import dataclasses
import datetime
import json
import pathlib

from notifier.sources import Moment

# A month after its moment, a key has done its job: nothing can still be pending for it.
# Keeping it forever is what breaks the next season, whose gameweek ids start again at 1.
KEEP_SENT_FOR = datetime.timedelta(days=30)


@dataclasses.dataclass
class State:
    # Keyed by alert key, valued by the ISO instant of the moment it belongs to. A plain
    # set is not enough: FPL's gameweek ids restart at 1 every season, so without a date
    # to prune on, this season's keys would silence next season's alerts invisibly --
    # no message, no retirement, nothing in the log to notice it by.
    sent: dict[str, str]
    # Per source ("fpl" / "draft") rather than merged, so one API failing can fall back
    # to its own cached half while the other stays fresh. Splitting a merged list back
    # apart would mean guessing which game contributed which moment.
    cached: dict[str, list[Moment]]
    # The next getUpdates offset. None until the first successful poll, matching
    # Telegram's own "omit it" convention for a fresh bot with nothing to confirm yet.
    update_offset: int | None = None
    # Chats already sent the one-time intro, keyed by str(chat_id). Never pruned like
    # `sent` is: a chat greeted once should stay greeted for the life of the bot, not
    # just for KEEP_SENT_FOR -- there is no future event that makes greeting it again
    # correct.
    greeted: set[str] = dataclasses.field(default_factory=set)
    # Chats seen in an add event but not yet successfully greeted: chat id to title,
    # with "" standing in for "no title" -- the one lossy part of this record, since a
    # JSON string can't distinguish "absent" from "empty" and Telegram never sends an
    # actually-empty one. An add moves a chat in here *before* any send is attempted,
    # and only out again once the send has actually succeeded. That ordering is the
    # fix for a real bug: `_greet` used to advance `update_offset` first and then try
    # to send, so a failed send was silently confirmed away -- Telegram never
    # redelivers an update once a higher offset has been acknowledged, so a chat whose
    # intro failed to send was never seen again. Keeping "who still needs greeting"
    # here, independent of the offset, is what makes retry genuine rather than
    # accidental.
    pending_greetings: dict[str, str] = dataclasses.field(default_factory=dict)
    # True once getUpdates has failed in a way no later tick will fix (a bad or
    # revoked token, or a webhook registered on this token -- see
    # `updates.PermanentPollError`). Deliberately absent from `save_state`'s payload
    # and never restored by `load_state`: the only real fix for either cause is to
    # change the token or remove the webhook and restart, and restarting is exactly
    # what resets this back to False. Persisting it would mean a *fixed* token still
    # couldn't poll again until someone noticed and hand-edited the state file.
    polling_disabled: bool = False


def _moment_to_json(moment: Moment) -> dict:
    return {
        "kind": moment.kind,
        "gw": moment.gw,
        "when": moment.when.isoformat(),
        "games": sorted(moment.games),
    }


def _moment_from_json(raw: dict) -> Moment:
    when = datetime.datetime.fromisoformat(raw["when"])
    if when.tzinfo is None:
        # Would raise TypeError inside due_alerts on the first tick after a restart.
        raise ValueError(f"cached moment has a naive datetime: {raw['when']!r}")
    games = raw["games"]
    if not isinstance(games, list) or not games:
        # A hand-edited `"games": "fpl"` would become {"f","p","l"} and mint keys like
        # `deadline:2:f:24` without raising. An empty list yields no keys at all, which
        # reads downstream as "already sent" and silences that alert permanently.
        raise ValueError(f"cached moment has a bad games list: {games!r}")
    return Moment(raw["kind"], int(raw["gw"]), when, frozenset(games))


def prune_sent(raw_sent: object, now: datetime.datetime) -> dict[str, str]:
    """Keep only keys whose moment is within KEEP_SENT_FOR of `now`.

    Every entry is shape-checked and degrades by omission rather than raising, matching
    this module's own discipline: a hand-edited or stale entry costs one possible
    duplicate send, never a refusal to start.
    """
    if not isinstance(raw_sent, dict):
        # A state file from before this change has `sent` as a list, which is not a
        # dict here and so degrades to empty. That is deliberate: the one-time cost is
        # that an alert already sent for a still-future moment might be sent again,
        # which is far cheaper than the alternative -- a store that, left unpruned,
        # never sends again once gameweek ids wrap around for the new season.
        return {}
    pruned: dict[str, str] = {}
    cutoff = now - KEEP_SENT_FOR
    for key, stamp in raw_sent.items():
        if not isinstance(key, str) or not isinstance(stamp, str):
            continue
        try:
            instant = datetime.datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if instant.tzinfo is None:
            continue
        if instant >= cutoff:
            pruned[key] = stamp
    return pruned


def _load_update_offset(raw: object) -> int | None:
    # bool is an int subclass; a stray `true` surviving a hand edit must not become
    # offset 1 and silently drop update_id 0.
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return None


def _load_greeted(raw: object) -> set[str]:
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if isinstance(item, str)}


def _load_pending_greetings(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        key: title for key, title in raw.items()
        if isinstance(key, str) and isinstance(title, str)
    }


def load_state(path: pathlib.Path, now: datetime.datetime) -> State:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return State(sent={}, cached={})
    if not isinstance(raw, dict):
        return State(sent={}, cached={})

    # Each field is shape-checked rather than trusted. A file that is valid JSON but
    # the wrong shape -- a hand edit, or the residue of a future schema change -- would
    # otherwise raise from inside set() or .items(), and raising is the one thing this
    # loader must not do: refusing to start over a file it could simply ignore is how a
    # deadline gets missed.
    sent = prune_sent(raw.get("sent"), now)

    raw_cached = raw.get("cached")
    cached: dict[str, list[Moment]] = {}
    if isinstance(raw_cached, dict):
        for game, entries in raw_cached.items():
            if not isinstance(entries, list):
                continue
            try:
                cached[game] = [_moment_from_json(entry) for entry in entries]
            except (KeyError, TypeError, ValueError):
                # Drop this source's cache only. The other source and the sent keys are
                # still good, so a schema change costs a refetch, not a duplicate storm.
                continue
    return State(
        sent=sent,
        cached=cached,
        update_offset=_load_update_offset(raw.get("update_offset")),
        greeted=_load_greeted(raw.get("greeted")),
        pending_greetings=_load_pending_greetings(raw.get("pending_greetings")),
        # polling_disabled is deliberately not restored here -- see the field's own
        # comment on State. Every fresh load starts able to poll again.
    )


def save_state(path: pathlib.Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sent": state.sent,
        "cached": {
            game: [_moment_to_json(m) for m in moments]
            for game, moments in state.cached.items()
        },
        "update_offset": state.update_offset,
        "greeted": sorted(state.greeted),
        "pending_greetings": state.pending_greetings,
        # polling_disabled is intentionally not written -- see the field's own
        # comment on State.
    }
    # Write and rename, so a crash mid-write leaves the previous file intact rather
    # than a truncated one. The temp sits in the same directory to keep the rename on
    # one filesystem, where it is atomic.
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    temp.replace(path)
