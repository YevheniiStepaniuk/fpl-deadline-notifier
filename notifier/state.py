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


@dataclasses.dataclass
class State:
    sent: set[str]
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
    return Moment(raw["kind"], int(raw["gw"]), when, frozenset(raw["games"]))


def load_state(path: pathlib.Path) -> State:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return State(sent=set(), cached={})
    if not isinstance(raw, dict):
        return State(sent=set(), cached={})

    # Each field is shape-checked rather than trusted. A file that is valid JSON but
    # the wrong shape -- a hand edit, or the residue of a future schema change -- would
    # otherwise raise from inside set() or .items(), and raising is the one thing this
    # loader must not do: refusing to start over a file it could simply ignore is how a
    # deadline gets missed.
    raw_sent = raw.get("sent")
    sent = (
        {key for key in raw_sent if isinstance(key, str)}
        if isinstance(raw_sent, list)
        else set()
    )

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
        "sent": sorted(state.sent),
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
