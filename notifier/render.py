"""Alerts to Telegram message text. Pure.

The only place a UTC instant becomes a local one. Everything upstream is UTC, so this
module is also the only place a clock-change bug can live.
"""

import datetime
import zoneinfo
from collections.abc import Sequence

from notifier.schedule import Alert
from notifier.sources import Moment

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


def _format_when(when: datetime.datetime, tz: zoneinfo.ZoneInfo) -> str:
    # %Z resolves through zoneinfo, so this prints BST or GMT according to the date
    # rather than a hardcoded offset that would be an hour wrong for half the season.
    return when.astimezone(tz).strftime("%a %d %b, %H:%M %Z")


def _when(alert: Alert, tz: zoneinfo.ZoneInfo) -> str:
    return _format_when(alert.moment.when, tz)


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


def format_intro(
    chat_id: int,
    title: str | None,
    next_deadline: Moment | None,
    tz: zoneinfo.ZoneInfo,
) -> str:
    """The one-time message sent to a chat the instant it adds the bot.

    This is often the only thing the chat ever hears from the bot unprompted: alerts
    still go only to `cfg.chat_id`, so a channel that has just added the bot gets
    nothing further unless someone copies the id back into `.env`. That is what this
    message is for, which is why it states the id rather than just saying hello.
    """
    where = f' to "{title}"' if title else ""
    lines = [
        f"⏰ I've been added{where}.",
        "I post FPL and Draft alerts: 24 hours and 2 hours before both the gameweek "
        "deadline and the Draft waiver deadline -- four alerts a gameweek.",
    ]
    if next_deadline is not None:
        lines.append(
            f"Next up: GW{next_deadline.gw} deadline, {_format_when(next_deadline.when, tz)}."
        )
    lines.append(
        f"This chat's id is {chat_id}. Set TELEGRAM_CHAT_ID to it in .env to receive "
        "alerts here -- alerts only ever go to the id configured there, not to every "
        "chat that adds the bot."
    )
    return "\n\n".join(lines)
