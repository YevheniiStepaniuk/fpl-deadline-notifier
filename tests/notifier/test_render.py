import datetime
import zoneinfo

from notifier.render import format_message
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
