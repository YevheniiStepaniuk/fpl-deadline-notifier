"""Configuration for the notifier, read from the environment on demand.

Deliberately unlike `app/config.py`, which reads `os.environ` at import. That made a
developer's `.env` invisible to `monkeypatch` and forced the session-wide neutralising
fixture in `tests/conftest.py`. Here the environment is read when `load_config` is
called, so a test passes a plain dict and nothing global is involved.
"""

import dataclasses
import os
import pathlib
import zoneinfo
from collections.abc import Mapping

from notifier.ai_banter import parse_aliases
from notifier.banter import RosterMember, parse_roster


class ConfigError(Exception):
    """Raised at startup when the environment cannot produce a usable Config."""


@dataclasses.dataclass(frozen=True)
class Config:
    bot_token: str
    chat_id: str
    tz: zoneinfo.ZoneInfo
    poll_seconds: float
    refresh_seconds: float
    state_path: pathlib.Path
    # Defaulted, unlike everything above: both are optional configuration for a
    # decoration on the alert, not the alert itself, so every existing direct
    # `Config(...)` call site -- test or otherwise -- keeps working unchanged.
    roster: tuple[RosterMember, ...] = ()
    banter_enabled: bool = True
    # The AI banter's own configuration, all defaulted for the same reason as above:
    # every existing `Config(...)` call site keeps working, and a deployment that sets
    # none of these keeps exactly the behaviour it had -- the twenty static lines.
    fpl_league_id: int | None = None
    draft_league_id: int | None = None
    openrouter_key: str = ""
    openrouter_model: str = "anthropic/claude-sonnet-5"
    ai_banter_enabled: bool = True
    # Telegram handle -> the manager name the league API publishes, as pairs rather than
    # a dict so Config stays frozen and hashable. Only needed where the two share no
    # letters; see ai_banter.link_roster for what it manages without one.
    aliases: tuple[tuple[str, str], ...] = ()

    @property
    def ai_banter_ready(self) -> bool:
        """Whether an AI line can even be attempted.

        Four things have to be true, and it is worth having them in one place rather
        than as a condition in `__main__`: banter at all, the AI variant not switched
        off, a key to spend, and at least one league id to be rude about. Missing any
        of them is not an error -- it is the static lines, which is what this service
        shipped with.
        """
        return bool(
            self.banter_enabled
            and self.ai_banter_enabled
            and self.openrouter_key
            and (self.fpl_league_id or self.draft_league_id)
        )


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        # Blank counts as missing: .env.example ships these keys with empty values, so
        # copying the template without filling it in is the likeliest way to get here.
        raise ConfigError(
            f"{name} is not set. Create a bot with @BotFather for the token, and "
            f"message @userinfobot for your chat id. See .env.example."
        )
    return value


def _seconds(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _switch(env: Mapping[str, str], name: str) -> bool:
    """An on/off variable, defaulting to on when unset. Shared by NOTIFIER_BANTER and
    NOTIFIER_BANTER_AI so the two switches cannot drift apart in what they accept."""
    raw = env.get(name, "").strip().lower()
    if not raw or raw == "on":
        return True
    if raw == "off":
        return False
    # Unlike a malformed NOTIFIER_ROSTER entry (see parse_roster), this is not
    # "other people's data" the operator cannot easily fix -- it is their own typo
    # in a two-value switch, so it gets the same treatment as a bad NOTIFIER_TZ:
    # named and refused rather than silently guessed at.
    raise ConfigError(f"{name} must be 'on' or 'off', got {raw!r}")


def _league_id(env: Mapping[str, str], name: str) -> int | None:
    """A league id, or None when unset.

    Strict rather than tolerant, unlike `parse_roster`: this is the operator's own
    number copied out of a URL they were looking at, and a silently ignored typo means
    the AI banter never once fires with no clue as to why. Zero is refused too -- no
    league has that id, and it would otherwise read as "configured" while fetching
    nothing.
    """
    raw = env.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number (the id in the league URL), got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a positive league id, got {value}")
    return value


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    tz_name = env.get("NOTIFIER_TZ", "").strip() or "Europe/London"
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception as exc:
        raise ConfigError(f"NOTIFIER_TZ is not a known timezone: {tz_name}") from exc
    return Config(
        bot_token=_required(env, "TELEGRAM_BOT_TOKEN"),
        chat_id=_required(env, "TELEGRAM_CHAT_ID"),
        tz=tz,
        poll_seconds=_seconds(env, "NOTIFIER_POLL_SECONDS", 60.0),
        refresh_seconds=_seconds(env, "NOTIFIER_REFRESH_SECONDS", 3600.0),
        state_path=pathlib.Path(
            env.get("NOTIFIER_STATE_PATH", "").strip() or "data/notifier_state.json"
        ),
        # Other people's Telegram handles, not this repo's business to hardcode --
        # see parse_roster's own docstring for why a bad entry is skipped rather
        # than refused.
        roster=parse_roster(env.get("NOTIFIER_ROSTER", "")),
        banter_enabled=_switch(env, "NOTIFIER_BANTER"),
        fpl_league_id=_league_id(env, "NOTIFIER_FPL_LEAGUE_ID"),
        draft_league_id=_league_id(env, "NOTIFIER_DRAFT_LEAGUE_ID"),
        # Shared with the dashboard this notifier was extracted from, which is why the
        # name carries no NOTIFIER_ prefix: one key in one .env, not two that can
        # disagree. Absent simply means no AI banter -- see Config.ai_banter_ready.
        openrouter_key=env.get("OPENROUTER_API_KEY", "").strip(),
        # NOTIFIER_BANTER_MODEL wins over OPENROUTER_MODEL, which is the shared one.
        # Without the override, picking a model for a one-line joke would also repoint
        # the dashboard's prose -- two features with very different needs (this one
        # wants cheap and mean, in one sentence) reading one variable.
        openrouter_model=(
            env.get("NOTIFIER_BANTER_MODEL", "").strip()
            or env.get("OPENROUTER_MODEL", "").strip()
            or "anthropic/claude-sonnet-5"
        ),
        ai_banter_enabled=_switch(env, "NOTIFIER_BANTER_AI"),
        # Other people's names paired with other people's handles: skipped one at a
        # time when malformed, exactly like NOTIFIER_ROSTER, and for the same reason.
        aliases=parse_aliases(env.get("NOTIFIER_BANTER_ALIASES", "")),
    )
