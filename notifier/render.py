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


def _games(moment: Moment) -> str:
    # Takes the `Moment` itself, not an `Alert`, so `format_next` -- which has a
    # moment but no scheduled alert to wrap it in -- can call this too.
    names = [_GAME_LABELS[g] for g in _GAME_ORDER if g in moment.games]
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
    return f"{headline}\n{_games(alert.moment)} · {_when(alert, tz)}"


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
    title: str | None,
    next_deadline: Moment | None,
    tz: zoneinfo.ZoneInfo,
) -> str:
    """The one-time message sent to a chat the instant it adds the bot.

    Used to end by stating the chat's own numeric id and telling the reader to set
    TELEGRAM_CHAT_ID in `.env` -- correct instructions for the operator, but this
    message lands in a shared group full of people who are not the operator, so it
    read as noise to everyone else. The id has not become unknowable, just relocated:
    it is still exactly what the operator needs to point alerts here, so it goes to a
    log line instead (see `_poll_and_greet` in `__main__.py`), where the operator
    actually is, rather than into the chat, where they mostly are not. Takes no
    `chat_id` parameter any more for the same reason -- nothing left in here needs it.
    """
    where = "this chat" if not title else f'"{title}"'
    lines = [
        f"⏰ Hi! I'll keep {where} posted on Fantasy Premier League deadlines.",
        "You'll get four reminders each gameweek — a day and two hours before the "
        "transfer deadline, and the same before the Draft waiver window closes.",
    ]
    if next_deadline is not None:
        lines.append(
            f"Next up: GW{next_deadline.gw} deadline, {_format_when(next_deadline.when, tz)}."
        )
    lines.append("Send /nextdeadline any time to see what's coming.")
    return "\n\n".join(lines)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _format_delta(delta: datetime.timedelta) -> str:
    """Real time remaining, for /nextdeadline -- deliberately not the scheduled
    alert's "{offset} hours" wording (see `_WHAT`), which is only true the instant an
    alert fires exactly on its 24h/2h trigger. Asked on demand, the true remaining
    time is whatever it actually is; reusing the canned offset here would be wrong
    almost every time it was shown.

    Rounds down to the coarsest whole unit worth naming (hours+minutes, then minutes,
    then seconds) rather than showing every unit down to the second once the gap is
    large -- "in 9 hours 12 minutes" is useful, "in 9 hours 12 minutes 47 seconds" is
    not. Flooring rather than rounding to the nearest unit is the safe direction for a
    countdown: it never claims less time is left than there actually is.
    """
    seconds = max(int(delta.total_seconds()), 0)
    if seconds == 0:
        # A moment landing exactly on the instant asked -- not "in 0 seconds", which
        # reads like a typo, and not negative, which `max(..., 0)` above already rules
        # out for a moment that has technically just passed by the time this runs.
        return "right now"
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        parts = [_plural(hours, "hour")]
        if minutes:
            parts.append(_plural(minutes, "minute"))
        return "in " + " ".join(parts)
    if minutes:
        return f"in {_plural(minutes, 'minute')}"
    return f"in {_plural(seconds, 'second')}"


def format_next(moment: Moment | None, now: datetime.datetime, tz: zoneinfo.ZoneInfo) -> str:
    """The on-demand reply to /nextdeadline. Pure, like `format_message`, and reuses
    the same `_format_when`/`_GAME_LABELS` machinery so the wall-clock time it states
    never disagrees with what a real alert would say about the same moment -- only the
    "how soon" phrasing differs, and deliberately so (see `_format_delta`).
    """
    if moment is None:
        # Between seasons, or every deadline already past for this one -- pleasant
        # rather than an empty string, since this is a direct reply to someone asking,
        # not a scheduled message that can just as easily not be sent at all.
        return "⏰ Nothing on the horizon right now — check back closer to the next gameweek."
    kind = "waiver window closes" if moment.kind == "waivers" else "deadline"
    headline = f"GW{moment.gw} {kind} {_format_delta(moment.when - now)}"
    return f"⏰ {headline}\n{_games(moment)} · {_format_when(moment.when, tz)}"
