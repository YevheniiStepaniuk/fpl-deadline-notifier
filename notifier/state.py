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
    return State(sent=sent, cached=cached)


def save_state(path: pathlib.Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sent": state.sent,
        "cached": {
            game: [_moment_to_json(m) for m in moments]
            for game, moments in state.cached.items()
        },
    }
    # Write and rename, so a crash mid-write leaves the previous file intact rather
    # than a truncated one. The temp sits in the same directory to keep the rename on
    # one filesystem, where it is atomic.
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    temp.replace(path)
