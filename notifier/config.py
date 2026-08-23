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
    )
