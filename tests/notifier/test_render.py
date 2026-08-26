import datetime
import zoneinfo

from notifier.render import format_intro, format_message, format_next
from notifier.schedule import Alert
from notifier.sources import Moment

LONDON = zoneinfo.ZoneInfo("Europe/London")
KYIV = zoneinfo.ZoneInfo("Europe/Kyiv")

DEADLINE = datetime.datetime(2026, 8, 28, 17, 30, tzinfo=datetime.UTC)
WAIVERS = datetime.datetime(2026, 8, 27, 17, 30, tzinfo=datetime.UTC)

GW2_DEADLINE = Moment("deadline", 2, DEADLINE, frozenset({"fpl", "draft"}))
GW2_WAIVERS = Moment("waivers", 2, WAIVERS, frozenset({"draft"}))


def test_a_single_deadline_alert_renders_in_full():
    text = format_message([Alert(GW2_DEADLINE, 2)], LONDON)
    assert text == "⏰ GW2 deadline in 2 hours\nFPL + Draft · Fri 28 Aug, 18:30 BST"


def test_the_day_before_alert_says_24_hours():
    text = format_message([Alert(GW2_DEADLINE, 24)], LONDON)
    assert "GW2 deadline in 24 hours" in text


def test_a_waiver_alert_names_the_waiver_window():
    """'deadline' would be ambiguous: Draft has two, and the waiver one is the whole
    reason this service reads the Draft API at all."""
    text = format_message([Alert(GW2_WAIVERS, 2)], LONDON)
    assert "GW2 waiver window closes in 2 hours" in text
    assert text.splitlines()[1].startswith("Draft · ")


def test_an_fpl_only_moment_names_only_fpl():
    """A divergence between the games has to be visible in the message rather than
    hidden by the merge."""
    fpl_only = Moment("deadline", 2, DEADLINE, frozenset({"fpl"}))
    assert format_message([Alert(fpl_only, 2)], LONDON).splitlines()[1].startswith("FPL · ")


def test_the_games_are_listed_in_a_stable_order():
    """frozenset iteration order is not guaranteed across runs, and a message that
    alternates between 'FPL + Draft' and 'Draft + FPL' looks broken."""
    swapped = Moment("deadline", 2, DEADLINE, frozenset({"draft", "fpl"}))
    assert "FPL + Draft" in format_message([Alert(swapped, 2)], LONDON)


def test_a_time_after_the_october_clock_change_renders_as_gmt():
    """GW9 is 2026-10-31T11:00Z, which is after the 25 October change, so London is on
    GMT and the local time equals the UTC time. A hardcoded +1 would put this an hour
    out and only be caught in November."""
    gw9 = Moment("deadline", 9, datetime.datetime(2026, 10, 31, 11, 0, tzinfo=datetime.UTC),
                 frozenset({"fpl", "draft"}))
    text = format_message([Alert(gw9, 2)], LONDON)
    assert "Sat 31 Oct, 11:00 GMT" in text


def test_the_timezone_argument_is_respected():
    text = format_message([Alert(GW2_DEADLINE, 2)], KYIV)
    assert "20:30" in text


def test_two_alerts_render_as_one_message_with_a_count_header():
    text = format_message([Alert(GW2_WAIVERS, 2), Alert(GW2_DEADLINE, 24)], LONDON)
    assert text.startswith("⏰ Two reminders")
    assert "GW2 waiver window closes in 2 hours" in text
    assert "GW2 deadline in 24 hours" in text


def test_three_or_more_alerts_fall_back_to_a_numeric_header():
    gw3 = Moment("deadline", 3, DEADLINE + datetime.timedelta(days=7), frozenset({"fpl"}))
    text = format_message(
        [Alert(GW2_WAIVERS, 2), Alert(GW2_DEADLINE, 24), Alert(gw3, 24)], LONDON
    )
    assert text.startswith("⏰ 3 reminders")


def test_alert_order_is_preserved():
    """due_alerts already sorted these by trigger time; the renderer must not resort."""
    text = format_message([Alert(GW2_DEADLINE, 24), Alert(GW2_WAIVERS, 2)], LONDON)
    assert text.index("deadline in 24 hours") < text.index("waiver window")


def test_an_empty_alert_list_renders_empty():
    """The loop must never call Telegram with this, but returning "" is a saner
    contract than raising from a formatter."""
    assert format_message([], LONDON) == ""


def test_the_intro_no_longer_mentions_configuration_details():
    """The owner's ask: the intro used to end by stating the chat's numeric id and
    telling the reader to set TELEGRAM_CHAT_ID in .env -- correct for the operator,
    noise for everyone else in a shared group who will never touch a .env file. None
    of that belongs in the message any more; see format_intro's own docstring for
    where the id went instead (a log line, not this text)."""
    text = format_intro("News", GW2_DEADLINE, LONDON)
    assert "-100123" not in text
    assert ".env" not in text
    assert "TELEGRAM_CHAT_ID" not in text


def test_the_intro_names_the_next_deadline_using_the_same_time_format_as_alerts():
    """Reuses format_message's own %Z formatting, so the intro and a real alert never
    disagree about what 18:30 BST looks like."""
    text = format_intro("News", GW2_DEADLINE, LONDON)
    assert "GW2" in text
    assert "Fri 28 Aug, 18:30 BST" in text


def test_the_intro_has_no_awkward_gap_when_there_is_no_next_deadline():
    """Between seasons there may be no future deadline at all."""
    text = format_intro("News", None, LONDON)
    assert "GW" not in text
    assert "\n\n\n" not in text


def test_the_intro_says_what_the_service_sends():
    """Four reminders a gameweek: a day and two hours before both the transfer
    deadline and the Draft waiver window."""
    text = format_intro("News", GW2_DEADLINE, LONDON)
    assert "24 hours" not in text  # the technical "offset_hours" phrasing is gone too
    assert "a day" in text
    assert "two hours" in text
    assert "waiver" in text.lower()


def test_the_intro_survives_a_chat_with_no_title():
    """Telegram does not send a title for every chat type; the message must still
    read sensibly with one missing, and must not crash on the None."""
    text = format_intro(None, None, LONDON)
    assert "this chat" in text


def test_the_intro_names_a_titled_chat():
    """A titled chat gets a warmer, more specific greeting than the untitled
    fallback -- pins that `title` still does something now that it is no longer
    spent on `format_intro`'s old 'added to "X"' sentence."""
    text = format_intro("News", None, LONDON)
    assert '"News"' in text


def test_the_intro_mentions_nextdeadline():
    """Change 2 adds the /nextdeadline command; the intro is where someone in the
    chat would learn it exists at all."""
    text = format_intro("News", GW2_DEADLINE, LONDON)
    assert "/nextdeadline" in text


# --- format_next ---------------------------------------------------------------------


def test_format_next_states_hours_and_minutes_remaining():
    """The whole point of format_next: real time remaining, not the scheduled alert's
    canned "in 24 hours" -- that phrasing is only true the instant an alert fires on
    schedule and would be wrong almost every time it was reused here."""
    now = DEADLINE - datetime.timedelta(hours=9, minutes=12, seconds=30)
    text = format_next(GW2_DEADLINE, now, LONDON)
    assert text == "⏰ GW2 deadline in 9 hours 12 minutes\nFPL + Draft · Fri 28 Aug, 18:30 BST"


def test_format_next_under_an_hour_has_no_hours_component():
    now = DEADLINE - datetime.timedelta(minutes=45)
    text = format_next(GW2_DEADLINE, now, LONDON)
    assert "in 45 minutes" in text
    assert "hour" not in text


def test_format_next_under_a_minute_counts_seconds():
    now = DEADLINE - datetime.timedelta(seconds=30)
    text = format_next(GW2_DEADLINE, now, LONDON)
    assert "in 30 seconds" in text


def test_format_next_singular_units_are_not_pluralised():
    """1 hour, 1 minute, 1 second must each read as singular -- "1 hours" is the kind
    of thing a naive f-string ships by accident."""
    assert "in 1 hour\n" in format_next(GW2_DEADLINE, DEADLINE - datetime.timedelta(hours=1), LONDON)
    assert "in 1 minute\n" in format_next(GW2_DEADLINE, DEADLINE - datetime.timedelta(minutes=1), LONDON)
    assert "in 1 second\n" in format_next(GW2_DEADLINE, DEADLINE - datetime.timedelta(seconds=1), LONDON)


def test_format_next_names_days_for_a_gap_of_a_week_or_so():
    """The common case, not an edge case: once a deadline passes, the next one is
    typically 5-6 days out, which is where format_next used to sit for most of any
    week before the day tier existed -- "in 143 hours 59 minutes" was unreadable, and
    this is what replaced it."""
    now = DEADLINE - datetime.timedelta(days=5, hours=23)
    text = format_next(GW2_DEADLINE, now, LONDON)
    assert text == "⏰ GW2 deadline in 5 days 23 hours\nFPL + Draft · Fri 28 Aug, 18:30 BST"


def test_format_next_drops_the_hours_component_when_it_rounds_to_zero():
    """Exactly 2 days out (or any exact multiple of 24h) must read "in 2 days", not
    "in 2 days 0 hours" -- the same zero-suppression the hours/minutes tier already
    had, extended to the new tier above it."""
    text = format_next(GW2_DEADLINE, DEADLINE - datetime.timedelta(hours=48), LONDON)
    assert "in 2 days" in text
    assert "0 hour" not in text


def test_format_next_at_exactly_24_hours_says_1_day():
    """The boundary the spec calls out by name: at precisely T-24h, this used to
    print "in 24 hours" (and coincidentally matched the scheduled alert's own
    wording, which is the old anchor `test_format_next_does_not_reuse_the_scheduled_
    alerts_wording` used before this test existed) -- now it must say "in 1 day", not
    "in 24 hours" and not "in 1 day 0 hours"."""
    text = format_next(GW2_DEADLINE, DEADLINE - datetime.timedelta(hours=24), LONDON)
    assert "in 1 day" in text
    assert "24 hours" not in text
    assert "0 hour" not in text


def test_format_next_just_under_24_hours_still_says_hours_not_a_day():
    """One minute below the day boundary must still use the hours+minutes tier --
    pins the boundary from the other side of the test above."""
    text = format_next(GW2_DEADLINE, DEADLINE - datetime.timedelta(hours=23, minutes=59), LONDON)
    assert "in 23 hours 59 minutes" in text
    assert "day" not in text


def test_format_next_a_day_and_some_hours_out():
    """The two-unit shape above the day tier: days + hours, singular "day" for the
    first unit and plural "hours" for the second."""
    text = format_next(GW2_DEADLINE, DEADLINE - datetime.timedelta(days=1, hours=2), LONDON)
    assert "in 1 day 2 hours" in text


def test_format_next_on_the_exact_boundary_says_right_now():
    """now == moment.when is the awkward case the spec calls out by name: "in 0
    seconds" would read like a typo, and a naive computation could even go negative."""
    text = format_next(GW2_DEADLINE, DEADLINE, LONDON)
    assert "right now" in text
    assert "in 0" not in text
    assert "in -" not in text


def test_format_next_names_a_waiver_window_not_a_deadline():
    now = WAIVERS - datetime.timedelta(hours=2)
    text = format_next(GW2_WAIVERS, now, LONDON)
    assert "waiver window closes" in text
    assert text.splitlines()[1].startswith("Draft · ")


def test_format_next_with_no_upcoming_moment_is_pleasant():
    """Between seasons, or after the last gameweek's last deadline, there may be
    nothing left to report -- this must read as a normal reply, not an error."""
    text = format_next(None, DEADLINE, LONDON)
    assert text
    assert "GW" not in text


def test_format_next_does_not_reuse_the_scheduled_alerts_wording():
    """The spec's explicit requirement: the on-demand reply must never say "in 24
    hours" or "in 2 hours" the way a scheduled alert does, since those are only true
    the instant an alert fires exactly on its trigger. Anchored on the 2-hour offset,
    not the 24-hour one: since the day tier was added, T-24h now floors to "1 day"
    (see test_format_next_at_exactly_24_hours_says_1_day), so it can no longer stand
    in for the "coincidentally matches, but only this once" case -- the 2-hour offset
    still can, because it never crosses the day boundary."""
    now = DEADLINE - datetime.timedelta(hours=2)
    text = format_next(GW2_DEADLINE, now, LONDON)
    assert "in 2 hours" in text  # coincidentally true this once, at exactly T-2h ...
    now = DEADLINE - datetime.timedelta(hours=1, minutes=59)
    text = format_next(GW2_DEADLINE, now, LONDON)
    assert "in 2 hours" not in text  # ... but false one minute later, unlike format_message
