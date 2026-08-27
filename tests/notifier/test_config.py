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
            "@alice_fpl:Chelsea,@bob_fpl:Manchester United,"
            "@carol_fpl:Liverpool,@dave_fpl:Arsenal"
        ),
    })
    assert cfg.roster == (
        RosterMember("@alice_fpl", "Chelsea"),
        RosterMember("@bob_fpl", "Manchester United"),
        RosterMember("@carol_fpl", "Liverpool"),
        RosterMember("@dave_fpl", "Arsenal"),
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


# ------------------------------------------------------- the AI banter's own settings

# These tests run after `test_config_reads_the_environment_when_called_not_when_imported`
# reloads notifier.config, and a reload rebinds ConfigError to a *new* class while the
# `load_config` imported at the top of this file keeps raising whatever the module dict
# holds now -- the exact trap notifier/__main__.py documents. So both names are resolved
# through the module at call time here rather than captured at import.
def _load(env):
    import notifier.config

    return notifier.config.load_config(env)


def _config_error():
    import notifier.config

    return notifier.config.ConfigError



def test_ai_banter_is_unconfigured_by_default():
    """A deployment that sets none of the new variables keeps exactly the behaviour it
    had: the twenty static lines."""
    cfg = _load(MINIMAL)
    assert cfg.fpl_league_id is None
    assert cfg.draft_league_id is None
    assert cfg.openrouter_key == ""
    assert cfg.openrouter_model == "anthropic/claude-sonnet-5"
    assert cfg.ai_banter_enabled is True
    assert cfg.ai_banter_ready is False


def test_league_ids_are_read_as_numbers():
    cfg = _load(MINIMAL | {"NOTIFIER_FPL_LEAGUE_ID": " 314 ", "NOTIFIER_DRAFT_LEAGUE_ID": "7"})
    assert (cfg.fpl_league_id, cfg.draft_league_id) == (314, 7)


@pytest.mark.parametrize("value", ["abc", "314/standings", "3.5", "-1", "0"])
def test_a_bad_league_id_is_named_in_the_error(value):
    """Strict rather than tolerant: this is the operator's own number copied from a URL,
    and a silently ignored typo means the feature never fires with no clue why."""
    with pytest.raises(_config_error()) as excinfo:
        _load(MINIMAL | {"NOTIFIER_FPL_LEAGUE_ID": value})
    assert "NOTIFIER_FPL_LEAGUE_ID" in str(excinfo.value)


def test_openrouter_model_can_be_overridden():
    cfg = _load(MINIMAL | {"OPENROUTER_MODEL": "openai/gpt-4o-mini"})
    assert cfg.openrouter_model == "openai/gpt-4o-mini"


def test_the_banter_model_override_wins_over_the_shared_one():
    """Otherwise choosing a model for a one-line joke also repoints the dashboard's
    prose, which reads the same shared variable."""
    env = MINIMAL | {"OPENROUTER_MODEL": "anthropic/claude-sonnet-5",
                     "NOTIFIER_BANTER_MODEL": "deepseek/deepseek-v4-pro-0813"}
    assert _load(env).openrouter_model == "deepseek/deepseek-v4-pro-0813"


def test_a_blank_banter_model_override_falls_through_to_the_shared_one():
    env = MINIMAL | {"OPENROUTER_MODEL": "openai/gpt-4o-mini", "NOTIFIER_BANTER_MODEL": "  "}
    assert _load(env).openrouter_model == "openai/gpt-4o-mini"


def test_a_blank_openrouter_model_falls_back_to_the_default():
    assert _load(MINIMAL | {"OPENROUTER_MODEL": "   "}).openrouter_model == "anthropic/claude-sonnet-5"


AI_READY = {"OPENROUTER_API_KEY": "sk-test", "NOTIFIER_FPL_LEAGUE_ID": "42"}


def test_ai_banter_ready_needs_a_key_and_a_league():
    assert _load(MINIMAL | AI_READY).ai_banter_ready is True
    assert _load(MINIMAL | {"OPENROUTER_API_KEY": "sk-test"}).ai_banter_ready is False
    assert _load(MINIMAL | {"NOTIFIER_FPL_LEAGUE_ID": "42"}).ai_banter_ready is False


def test_a_draft_league_alone_is_enough():
    env = MINIMAL | {"OPENROUTER_API_KEY": "sk-test", "NOTIFIER_DRAFT_LEAGUE_ID": "7"}
    assert _load(env).ai_banter_ready is True


@pytest.mark.parametrize("value", ["off", "OFF", " Off "])
def test_banter_ai_off_is_honoured(value):
    assert _load(MINIMAL | AI_READY | {"NOTIFIER_BANTER_AI": value}).ai_banter_ready is False


def test_banter_off_switches_off_the_ai_variant_too():
    """One switch for the whole feature: NOTIFIER_BANTER=off means no line at all, not
    a static line replaced by an AI one."""
    assert _load(MINIMAL | AI_READY | {"NOTIFIER_BANTER": "off"}).ai_banter_ready is False


def test_an_unrecognised_banter_ai_value_is_named_in_the_error():
    with pytest.raises(_config_error()) as excinfo:
        _load(MINIMAL | {"NOTIFIER_BANTER_AI": "maybe"})
    assert "NOTIFIER_BANTER_AI" in str(excinfo.value)


def test_aliases_parse_into_handle_name_pairs():
    env = MINIMAL | {"NOTIFIER_BANTER_ALIASES": "@nine_iron=Ada Lovelace,@rexmarlow=Rex Marlow"}
    assert _load(env).aliases == (
        ("@nine_iron", "Ada Lovelace"),
        ("@rexmarlow", "Rex Marlow"),
    )


def test_unset_aliases_are_the_empty_tuple_not_an_error():
    assert _load(MINIMAL).aliases == ()


def test_a_malformed_alias_is_skipped_without_refusing_to_start():
    env = MINIMAL | {"NOTIFIER_BANTER_ALIASES": "@no_separator,@rexmarlow=Rex Marlow"}
    assert _load(env).aliases == (("@rexmarlow", "Rex Marlow"),)
