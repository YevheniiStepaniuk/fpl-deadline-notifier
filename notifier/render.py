"""Alerts to Telegram message text. Pure.

The only place a UTC instant becomes a local one. Everything upstream is UTC, so this
module is also the only place a clock-change bug can live.
"""

import zoneinfo
from collections.abc import Sequence

from notifier.schedule import Alert

# The official sites quote deadlines in these games' own names; matching them means the
# message agrees with the page you click through to.
_GAME_LABELS = {"fpl": "FPL", "draft": "Draft"}
# Fixed order, because frozenset iteration order is not stable across runs and a message
# that alternates between "FPL + Draft" and "Draft + FPL" reads as a bug.
_GAME_ORDER = ("fpl", "draft")

_WHAT = {
    "deadline": "GW{gw} deadline in {hours} hours",
    # "deadline" alone would be ambiguous: Draft has two, and the waiver one is the
    # reason this service reads the Draft API at all.
    "waivers": "GW{gw} waiver window closes in {hours} hours",
}

_COUNT_WORDS = {2: "Two"}


def _games(alert: Alert) -> str:
    names = [_GAME_LABELS[g] for g in _GAME_ORDER if g in alert.moment.games]
    return " + ".join(names)


def _when(alert: Alert, tz: zoneinfo.ZoneInfo) -> str:
    # %Z resolves through zoneinfo, so this prints BST or GMT according to the date
    # rather than a hardcoded offset that would be an hour wrong for half the season.
    local = alert.moment.when.astimezone(tz)
    return local.strftime("%a %d %b, %H:%M %Z")


def _block(alert: Alert, tz: zoneinfo.ZoneInfo) -> str:
    headline = _WHAT[alert.moment.kind].format(
        gw=alert.moment.gw, hours=alert.offset_hours
    )
    return f"{headline}\n{_games(alert)} · {_when(alert, tz)}"


def format_message(alerts: Sequence[Alert], tz: zoneinfo.ZoneInfo) -> str:
    """One message for every alert due in this tick.

    A list rather than a single alert because a rescheduled deadline can put two
    triggers in the same minute, and two notifications a second apart are worse than
    one with two lines. On an ordinary week the list has exactly one element.
    """
    if not alerts:
        return ""
    if len(alerts) == 1:
        return f"⏰ {_block(alerts[0], tz)}"
    count = _COUNT_WORDS.get(len(alerts), str(len(alerts)))
    blocks = "\n\n".join(_block(alert, tz) for alert in alerts)
    return f"⏰ {count} reminders\n\n{blocks}"
