import pathlib
import zoneinfo

import pytest

from notifier.banter import RosterMember
from notifier.config import Config, ConfigError, load_config

MINIMAL = {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "456"}


def test_minimal_env_gives_documented_defaults():
    cfg = load_config(MINIMAL)
    assert cfg.bot_token == "123:abc"
    assert cfg.chat_id == "456"
    assert cfg.tz == zoneinfo.ZoneInfo("Europe/London")
    assert cfg.poll_seconds == 60.0
    assert cfg.refresh_seconds == 3600.0
    assert cfg.state_path == pathlib.Path("data/notifier_state.json")
    assert cfg.roster == ()
    assert cfg.banter_enabled is True


def test_every_default_is_overridable():
    cfg = load_config(
        MINIMAL
        | {
            "NOTIFIER_TZ": "Europe/Kyiv",
            "NOTIFIER_POLL_SECONDS": "30",
            "NOTIFIER_REFRESH_SECONDS": "900",
            "NOTIFIER_STATE_PATH": "/tmp/s.json",
        }
    )
    assert cfg.tz == zoneinfo.ZoneInfo("Europe/Kyiv")
    assert cfg.poll_seconds == 30.0
    assert cfg.refresh_seconds == 900.0
    assert cfg.state_path == pathlib.Path("/tmp/s.json")


@pytest.mark.parametrize("missing", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
def test_a_missing_required_variable_is_named_in_the_error(missing):
    """A notifier that starts and silently never sends is worse than one that refuses
    to start, so the failure has to say which variable is absent."""
    env = {k: v for k, v in MINIMAL.items() if k != missing}
    with pytest.raises(ConfigError) as excinfo:
        load_config(env)
    assert missing in str(excinfo.value)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_required_variable_is_treated_as_missing(blank):
    """`.env.example` ships `TELEGRAM_BOT_TOKEN=` with no value. Copying it without
    filling it in must fail the same way deleting the line does."""
    with pytest.raises(ConfigError):
        load_config(MINIMAL | {"TELEGRAM_BOT_TOKEN": blank})


def test_an_unknown_timezone_is_named_in_the_error():
    with pytest.raises(ConfigError) as excinfo:
        load_config(MINIMAL | {"NOTIFIER_TZ": "Mars/Olympus_Mons"})
    assert "Mars/Olympus_Mons" in str(excinfo.value)


def test_a_non_numeric_interval_is_named_in_the_error():
    with pytest.raises(ConfigError) as excinfo:
        load_config(MINIMAL | {"NOTIFIER_POLL_SECONDS": "soon"})
    assert "NOTIFIER_POLL_SECONDS" in str(excinfo.value)


def test_the_owners_roster_parses_into_four_paired_members():
    """The exact value the owner will actually set NOTIFIER_ROSTER to."""
    cfg = load_config(MINIMAL | {
        "NOTIFIER_ROSTER": (
            "@thecouriersix:Chelsea,@romanusyk:Manchester United,"
            "@just_yuricle:Liverpool,@d_vodotiiets:Arsenal"
        ),
    })
    assert cfg.roster == (
        RosterMember("@thecouriersix", "Chelsea"),
        RosterMember("@romanusyk", "Manchester United"),
        RosterMember("@just_yuricle", "Liverpool"),
        RosterMember("@d_vodotiiets", "Arsenal"),
    )


def test_an_unset_roster_is_the_empty_tuple_not_an_error():
    """The spec's explicit callout: empty or unset is valid and means no roster."""
    assert load_config(MINIMAL).roster == ()
    assert load_config(MINIMAL | {"NOTIFIER_ROSTER": ""}).roster == ()


def test_a_malformed_roster_entry_is_skipped_without_refusing_to_start():
    """Tolerant parsing at the config layer too, not just in parse_roster's own
    tests -- this is the path load_config actually calls."""
    cfg = load_config(MINIMAL | {"NOTIFIER_ROSTER": "@good:Team,no-colon-here,@also:Fine"})
    assert cfg.roster == (RosterMember("@good", "Team"), RosterMember("@also", "Fine"))


@pytest.mark.parametrize("value", ["on", "ON", " on ", ""])
def test_banter_defaults_and_explicit_on_are_both_enabled(value):
    assert load_config(MINIMAL | {"NOTIFIER_BANTER": value}).banter_enabled is True


@pytest.mark.parametrize("value", ["off", "OFF", " off "])
def test_banter_off_is_honoured(value):
    assert load_config(MINIMAL | {"NOTIFIER_BANTER": value}).banter_enabled is False


def test_an_unrecognised_banter_value_is_named_in_the_error():
    with pytest.raises(ConfigError) as excinfo:
        load_config(MINIMAL | {"NOTIFIER_BANTER": "maybe"})
    assert "NOTIFIER_BANTER" in str(excinfo.value)


def test_config_is_frozen():
    cfg = load_config(MINIMAL)
    with pytest.raises(Exception):
        cfg.bot_token = "other"  # type: ignore[misc]


def test_load_config_defaults_to_the_process_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-os")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "789")
    cfg = load_config()
    assert cfg.bot_token == "from-os"


def test_config_reads_the_environment_when_called_not_when_imported(monkeypatch):
    """`app/config.py` reads os.environ at import, and tests/conftest.py carries a long
    comment about what that cost. The notifier must not repeat it: importing the module
    with no environment set has to be harmless, and a later setenv has to be visible.

    This reload is also what makes `notifier/__main__.py` import `notifier.config` as a
    module instead of unpacking `ConfigError` and `load_config` with `from ... import`.
    A reload rebinds every name in the reloaded module to new objects; a name copied out
    beforehand keeps pointing at the old one, so `except ConfigError` built from such a
    copy silently stops matching what `load_config` actually raises. It only shows up
    when both test modules run in the same session, so any future module that needs to
    catch `ConfigError` should resolve it through the module, not import it by name.
    """
    import importlib

    import notifier.config

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    importlib.reload(notifier.config)  # must not raise

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "set-after-import")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    assert notifier.config.load_config().bot_token == "set-after-import"
