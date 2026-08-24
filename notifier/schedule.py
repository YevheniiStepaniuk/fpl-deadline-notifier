"""Which alerts are owed right now. Pure: no clock, no network, no disk.

`now` is an argument rather than a call to datetime.now() precisely so that every rule
in here can be tested at an exact instant, including the boundaries.
"""

import dataclasses
import datetime
from collections.abc import Container, Iterable

from notifier.sources import Moment

# A day before and two hours before, and nothing else. Defined once so the pair cannot
# drift between the scheduler, the renderer and the documentation.
OFFSETS_HOURS: tuple[int, ...] = (24, 2)


@dataclasses.dataclass(frozen=True)
class Alert:
    moment: Moment
    offset_hours: int

    @property
    def keys(self) -> frozenset[str]:
        """Identity in the sent-state file: one key per game this alert covers.

        Per game rather than one key per alert, because `sources.merge` keeps FPL and
        Draft as separate moments whenever they publish different instants for the same
        gameweek. Under a single `{kind}:{gw}:{offset}` key those two moments would
        collide -- whichever fired first would write the key and the other would find it
        already there and never send, losing exactly the divergence merge preserves.
        Per-game keys also make the reverse safe: if the games diverge after a merged
        alert has gone out, both keys are already spent and neither half re-fires.

        Keyed by gameweek rather than by timestamp, so a deadline the Premier League
        moves by an hour does not read as a new alert and get sent twice.
        """
        return frozenset(
            f"{self.moment.kind}:{self.moment.gw}:{game}:{self.offset_hours}"
            for game in self.moment.games
        )

    @property
    def trigger(self) -> datetime.datetime:
        return self.moment.when - datetime.timedelta(hours=self.offset_hours)


def due_alerts(
    moments: Iterable[Moment],
    sent: Container[str],
    now: datetime.datetime,
) -> tuple[list[Alert], list[Alert]]:
    """Split the alerts whose trigger has passed into those worth sending and those not.

    Returns (to_send, to_retire). Both need writing to state on success; only the first
    needs a message. Two things get retired rather than sent: an alert whose moment has
    already passed, and an alert superseded by a more urgent one for the same moment.
    Between them they are what stops a restart after a long outage from delivering a
    burst of warnings that are either stale or wrong about the time remaining.
    """
    # utcoffset() rather than tzinfo, which is also non-None for the pathological
    # tzinfo whose utcoffset() returns None -- still naive for comparison purposes.
    if now.utcoffset() is None:
        raise TypeError("due_alerts needs a timezone-aware `now`; got a naive datetime")

    to_send: list[Alert] = []
    to_retire: list[Alert] = []
    for moment in moments:
        due = [
            alert
            for alert in (Alert(moment, offset) for offset in OFFSETS_HOURS)
            # Owed while *any* game's key is unsent: half-spent is not spent, and an
            # FPL alert having gone out must not silence Draft's.
            if any(key not in sent for key in alert.keys) and alert.trigger <= now
        ]
        if not due:
            continue
        # `<=` rather than `<`: a moment landing exactly on this tick offers zero
        # notice, which is not a warning.
        if moment.when <= now:
            to_retire.extend(due)
            continue
        # Both offsets come due together on a first run, or on a restart after an
        # outage longer than 22 hours. Sending both would announce "in 24 hours" about
        # a deadline two hours away. The smallest offset is the only one still true, so
        # the rest are retired -- retired rather than skipped, or they would be due
        # again on every tick from here to the deadline.
        urgent = min(due, key=lambda alert: alert.offset_hours)
        to_send.append(urgent)
        to_retire.extend(alert for alert in due if alert is not urgent)
    order = lambda alert: (alert.trigger, sorted(alert.keys))  # noqa: E731
    return sorted(to_send, key=order), sorted(to_retire, key=order)
