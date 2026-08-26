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

# An intro this late has lost its purpose -- by then whoever added the bot will have
# found the chat id another way, so there is nothing left to bound by keeping it. Also
# what stops a chat that fails every send (blocked, kicked, or just persistently down)
# from sitting in `pending_greetings` forever if nothing ever restarts the process to
# reload and re-check its age.
KEEP_PENDING_FOR = datetime.timedelta(days=1)


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
    # Chats seen in an add event but not yet successfully greeted: chat id to
    # `"{first_seen_iso}|{title}"` (see `encode_pending_greeting`/
    # `decode_pending_greeting`), split on the first "|" only so a title containing
    # one still survives. An add moves a chat in here *before* any send is attempted,
    # and only out again once the send has actually succeeded -- or is given up on as
    # permanently undeliverable, or ages out past `KEEP_PENDING_FOR`. That ordering is
    # the fix for a real bug: `_greet` used to advance `update_offset` first and then
    # try to send, so a failed send was silently confirmed away -- Telegram never
    # redelivers an update once a higher offset has been acknowledged, so a chat whose
    # intro failed to send was never seen again. Keeping "who still needs greeting"
    # here, independent of the offset, is what makes retry genuine rather than
    # accidental. Unlike `sent`, which `prune_sent` bounds, this dict would otherwise
    # grow without limit -- anyone who can add the bot creates an entry -- which is
    # what `first_seen`/`KEEP_PENDING_FOR` are for.
    pending_greetings: dict[str, str] = dataclasses.field(default_factory=dict)
    # True once getUpdates has failed in a way no later tick will fix (a bad or
    # revoked token, or a webhook registered on this token -- see
    # `updates.PermanentPollError`). Deliberately absent from `save_state`'s payload
    # and never restored by `load_state`: the only real fix for either cause is to
    # change the token or remove the webhook and restart, and restarting is exactly
    # what resets this back to False. Persisting it would mean a *fixed* token still
    # couldn't poll again until someone noticed and hand-edited the state file.
    polling_disabled: bool = False
    # Which of banter.py's line ids have been used in the current cycle, so a
    # restart resumes the cycle instead of silently starting a fresh one every
    # deploy -- see banter.next_banter for the cycle rule itself.
    banter_used: frozenset[str] = dataclasses.field(default_factory=frozenset)
    # The roster handle targeted by the most recent banter line, or None if that
    # line targeted nobody (one of the six generic lines) or none has run yet.
    # banter.next_banter needs this to enforce "never the same target twice in a
    # row" across a restart, not just within one process's lifetime.
    banter_last_target: str | None = None


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


def encode_pending_greeting(first_seen: datetime.datetime, title: str) -> str:
    """Pack a pending greeting's value: when the chat was first seen, plus its title.

    `partition("|")` on the way back out splits on the *first* separator only, so a
    title that happens to contain a "|" of its own still round-trips intact.
    """
    return f"{first_seen.isoformat()}|{title}"


def decode_pending_greeting(value: str) -> tuple[datetime.datetime, str] | None:
    """Inverse of `encode_pending_greeting`. None on anything unparseable -- a missing
    separator, or a first-seen stamp that is not a real aware datetime -- so a
    corrupted entry degrades the same way the rest of this module does rather than
    raising.
    """
    first_seen_raw, sep, title = value.partition("|")
    if not sep:
        return None
    try:
        first_seen = datetime.datetime.fromisoformat(first_seen_raw)
    except ValueError:
        return None
    if first_seen.tzinfo is None:
        return None
    return first_seen, title


def prune_pending_greetings(raw: object, now: datetime.datetime) -> dict[str, str]:
    """Keep only entries whose key is a chat id and whose value decodes to a
    first-seen stamp within `KEEP_PENDING_FOR` of `now`.

    Called from two places, exactly like `prune_sent`: once inside `load_state`, on
    the raw untrusted JSON, and once inside `run_once`'s tick, on the already-clean
    in-memory dict. `sent` needed both because pruning only at load meant a process
    that stays up across the June season boundary never pruned at all -- a review
    caught it and the fix added the tick-time call alongside the load-time one.
    `pending_greetings` is the same shape of bug for the same reason: a process that
    never restarts would otherwise never re-check a stale entry's age, and this is
    what makes both fields follow one policy instead of two.

    The key check is not cosmetic: `_greet` calls `int(key)` to build the chat id
    `format_intro` needs, and used to do that before ever attempting a send. One
    non-numeric key surviving load would raise there, and because that call sits
    inside `_greet`'s own broad except (see `__main__.py`), the exception would not
    just skip that one chat -- it would abort the whole greeting step for the tick,
    silently, behind a generic "failed unexpectedly" log line, taking every *other*
    pending chat down with it. Dropping the bad row here, the same place `prune_sent`
    drops a bad `sent` entry, is what keeps one corrupt row from costing every chat
    still waiting to be greeted.
    """
    if not isinstance(raw, dict):
        return {}
    cutoff = now - KEEP_PENDING_FOR
    pending: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        try:
            int(key)
        except ValueError:
            continue
        decoded = decode_pending_greeting(value)
        if decoded is None:
            continue
        first_seen, _title = decoded
        if first_seen >= cutoff:
            pending[key] = value
    return pending


def load_banter_state(raw_used: object, raw_last_target: object) -> tuple[frozenset[str], str | None]:
    """Restore `banter_used`/`banter_last_target`, or start a fresh cycle.

    Deliberately coarser than `prune_sent`'s per-entry salvage: banter is
    decoration, and a genuinely corrupt pair (the wrong shape, a non-string id) is
    not worth picking apart entry by entry the way a `sent` timestamp is -- the
    worst case of resetting both together is one repeated line and one repeated
    target sooner than the rules would otherwise allow, never a crash and never a
    missed alert. `next_banter` also re-derives eligibility from `LINE_IDS`/the
    roster on every call, so a used-id that no longer names a real line (a
    previous deploy's line pool, edited by hand) is harmless left in -- it just
    never matches anything in `eligible_lines()` and sits inert until the next
    reset.
    """
    if not isinstance(raw_used, list) or not all(isinstance(x, str) for x in raw_used):
        return frozenset(), None
    if raw_last_target is not None and not isinstance(raw_last_target, str):
        return frozenset(), None
    return frozenset(raw_used), raw_last_target


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
    banter_used, banter_last_target = load_banter_state(
        raw.get("banter_used"), raw.get("banter_last_target")
    )
    return State(
        sent=sent,
        cached=cached,
        update_offset=_load_update_offset(raw.get("update_offset")),
        greeted=_load_greeted(raw.get("greeted")),
        pending_greetings=prune_pending_greetings(raw.get("pending_greetings"), now),
        # polling_disabled is deliberately not restored here -- see the field's own
        # comment on State. Every fresh load starts able to poll again.
        banter_used=banter_used,
        banter_last_target=banter_last_target,
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
        # Sorted for the same reason `greeted` is: JSON has no set type, and a
        # stable on-disk order keeps the file's diffs sane across saves.
        "banter_used": sorted(state.banter_used),
        "banter_last_target": state.banter_last_target,
    }
    # Write and rename, so a crash mid-write leaves the previous file intact rather
    # than a truncated one. The temp sits in the same directory to keep the rename on
    # one filesystem, where it is atomic.
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    temp.replace(path)
