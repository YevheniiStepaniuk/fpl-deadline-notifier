"""Which alerts are owed right now. Pure: no clock, no network, no disk.

`now` is an argument rather than a call to datetime.now() precisely so that every rule
in here can be tested at an exact instant, including the boundaries.
"""

import dataclasses
import datetime
from collections.abc import Iterable, Set

from notifier.sources import Moment

# A day before and two hours before, and nothing else. Defined once so the pair cannot
# drift between the scheduler, the renderer and the documentation.
OFFSETS_HOURS: tuple[int, ...] = (24, 2)


@dataclasses.dataclass(frozen=True)
class Alert:
    moment: Moment
    offset_hours: int

    @property
    def key(self) -> str:
        """Identity in the sent-state file.

        Keyed by gameweek rather than by timestamp, so a deadline the Premier League
        moves by an hour does not read as a new alert and get sent twice.
        """
        return f"{self.moment.kind}:{self.moment.gw}:{self.offset_hours}"

    @property
    def trigger(self) -> datetime.datetime:
        return self.moment.when - datetime.timedelta(hours=self.offset_hours)


def due_alerts(
    moments: Iterable[Moment],
    sent: Set[str],
    now: datetime.datetime,
) -> tuple[list[Alert], list[Alert]]:
    """Split the alerts whose trigger has passed into those worth sending and those not.

    Returns (to_send, to_retire). Both need writing to state on success; only the first
    needs a message. Two things get retired rather than sent: an alert whose moment has
    already passed, and an alert superseded by a more urgent one for the same moment.
    Between them they are what stops a restart after a long outage from delivering a
    burst of warnings that are either stale or wrong about the time remaining.
    """
    if now.tzinfo is None:
        raise TypeError("due_alerts needs a timezone-aware `now`; got a naive datetime")

    to_send: list[Alert] = []
    to_retire: list[Alert] = []
    for moment in moments:
        due = [
            alert
            for alert in (Alert(moment, offset) for offset in OFFSETS_HOURS)
            if alert.key not in sent and alert.trigger <= now
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
    key = lambda alert: (alert.trigger, alert.key)  # noqa: E731
    return sorted(to_send, key=key), sorted(to_retire, key=key)
