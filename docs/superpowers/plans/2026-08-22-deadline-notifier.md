# Deadline Notifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A standalone always-on service that sends a Telegram message 24 hours and 2 hours before every Fantasy Premier League gameweek deadline and every Draft PL waiver deadline.

**Architecture:** A new top-level `notifier/` package that imports nothing from `app/`. A tick loop wakes every 60 seconds, refreshes both official APIs hourly, and asks a pure `schedule.due_alerts(...)` which alerts are owed. Everything worth arguing about lives in pure functions that take `now` as an argument; the network, the clock and the disk sit in thin shells around them.

**Tech Stack:** Python 3.13, `httpx` (already a dependency), `zoneinfo` (stdlib), `pytest` + `respx` (already dev dependencies). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-22-deadline-notifier-design.md`

## Global Constraints

- **No new dependencies.** `httpx`, `pytest` and `respx` are already declared in `pyproject.toml`. Nothing else may be added.
- **`notifier/` imports nothing from `app/`.** Not `app.config`, not `app.fpl_client`. The duplication is deliberate — see the spec's "Independence" section. A test enforces this in Task 1.
- **No import-time environment reads.** `notifier/config.py` must expose `load_config(env)` and read `os.environ` when called, never at import. `app/config.py` reads at import and `tests/conftest.py` carries a long comment about the damage that caused; do not repeat it.
- **All datetimes are timezone-aware.** UTC internally, converted to the display timezone only inside `render.py`. A naive `datetime` anywhere is a bug.
- **Python 3.13**, run everything through `.venv/bin/`. Never `pip install`; the venv is already provisioned.
- **Alert offsets are exactly `(24, 2)` hours.** Defined once, in `notifier/schedule.py`.
- **Moment kinds are exactly `"deadline"` and `"waivers"`.** `trades_time` is out of scope.
- **Game names are exactly `"fpl"` and `"draft"`.**
- **The sent-key format is `{kind}:{gw}:{offset_hours}`**, e.g. `waivers:2:24`.
- Run the full suite with `.venv/bin/pytest` before every commit. It must stay green — 27 existing test modules already pass.

---

## File Structure

| File | Responsibility |
|---|---|
| `notifier/__init__.py` | Empty package marker. |
| `notifier/config.py` | `Config` dataclass, `load_config(env)`, `ConfigError`. Reads env at call time. |
| `notifier/sources.py` | `Moment`, both API URLs, `parse_fpl`, `parse_draft`, `fetch_source`, `merge`. |
| `notifier/schedule.py` | `Alert`, `OFFSETS_HOURS`, `due_alerts`. Pure. |
| `notifier/render.py` | `format_message(alerts, tz)`. Pure. |
| `notifier/state.py` | `State`, `load_state`, `save_state`. JSON on disk. |
| `notifier/telegram.py` | `send_message`, `TelegramError`. Retries with backoff. |
| `notifier/__main__.py` | `run_once`, `main`. The tick loop and all the wiring. |
| `notifier/deploy/fpl-notifier.service` | systemd unit template, not installed. |
| `tests/notifier/__init__.py` | Test subpackage marker (matches `tests/news/`). |
| `tests/notifier/fixtures/*.json` | Trimmed real payloads captured 2026-08-22. |
| `tests/notifier/test_*.py` | One module per source module. |

Modified: `pyproject.toml` (packages.find), `.gitignore` (state file), `.env.example` (two variables), `README.md` (a run section).

---

### Task 1: Package skeleton, configuration, and project wiring

Creates the package, the config loader, and every project-level file the later tasks depend on. Grouped into one task because a half-wired package is not independently testable: the isolation test, the config test and the `pip install -e` path all need each other.

**Files:**
- Create: `notifier/__init__.py`
- Create: `notifier/config.py`
- Create: `tests/notifier/__init__.py`
- Create: `tests/notifier/test_config.py`
- Create: `tests/notifier/test_isolation.py`
- Modify: `pyproject.toml` (the `[tool.setuptools.packages.find]` block)
- Modify: `.gitignore`
- Modify: `.env.example`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `notifier.config.Config` — frozen dataclass with fields `bot_token: str`, `chat_id: str`, `tz: ZoneInfo`, `poll_seconds: float`, `refresh_seconds: float`, `state_path: Path`.
  - `notifier.config.load_config(env: Mapping[str, str] | None = None) -> Config` — `None` means `os.environ`.
  - `notifier.config.ConfigError(Exception)`.

- [ ] **Step 1: Create the package markers**

```bash
cd "/Users/yevheniistepaniuk/Documents/Personal Projects/Fantasy PL"
mkdir -p notifier tests/notifier/fixtures
touch notifier/__init__.py tests/notifier/__init__.py
```

- [ ] **Step 2: Write the failing config tests**

Create `tests/notifier/test_config.py`:

```python
import pathlib
import zoneinfo

import pytest

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
    with no environment set has to be harmless, and a later setenv has to be visible."""
    import importlib

    import notifier.config

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    importlib.reload(notifier.config)  # must not raise

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "set-after-import")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    assert notifier.config.load_config().bot_token == "set-after-import"
```

- [ ] **Step 3: Run the config tests to verify they fail**

Run: `.venv/bin/pytest tests/notifier/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'notifier.config'`

- [ ] **Step 4: Write `notifier/config.py`**

```python
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
```

- [ ] **Step 5: Run the config tests to verify they pass**

Run: `.venv/bin/pytest tests/notifier/test_config.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 6: Write the isolation test**

Create `tests/notifier/test_isolation.py`:

```python
import pathlib

_NOTIFIER = pathlib.Path(__file__).resolve().parents[2] / "notifier"


def test_no_module_in_notifier_imports_from_app():
    """The spec's Independence section: a refactor inside app/ must not be able to stop
    the alerts. The two packages share fetch logic by copy, on purpose. This test is
    what makes that a decision rather than an accident waiting to be undone."""
    offenders = []
    for path in sorted(_NOTIFIER.rglob("*.py")):
        source = path.read_text()
        for lineno, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("import app", "from app")):
                offenders.append(f"{path.name}:{lineno}: {stripped}")
    assert offenders == []
```

- [ ] **Step 7: Run the isolation test**

Run: `.venv/bin/pytest tests/notifier/test_isolation.py -v`
Expected: PASS (the package has one module and it imports nothing from `app`).

- [ ] **Step 8: Wire the package into `pyproject.toml`**

In `pyproject.toml`, the find block currently reads:

```toml
[tool.setuptools.packages.find]
include = ["app*"]
```

Change it to:

```toml
[tool.setuptools.packages.find]
include = ["app*", "notifier*"]
```

Without this the editable install exposes `app` only, and `python -m notifier` works from the
project root by accident of `sys.path` while failing anywhere else.

- [ ] **Step 9: Add the state file to `.gitignore`**

`.gitignore` ignores `data/*.sqlite3` and `data/*.db`, which does not cover the new JSON file.
Under the `# Local data` section, after the `data/*.db` line, add:

```gitignore
# The notifier's sent-alert keys and its last-good deadline cache. Nothing secret, but
# machine-local: committed, it would conflict on every pull and could suppress an alert
# on another machine by claiming it was already sent.
data/notifier_state.json
```

- [ ] **Step 10: Document the variables in `.env.example`**

Append to `.env.example`:

```
# --- Deadline notifier (the `notifier/` service, separate from the dashboard) ---
# Both required to start it; it refuses to run without them rather than sitting
# silently idle. Create a bot by messaging @BotFather and taking the token it prints;
# get your chat id by messaging @userinfobot. Send your bot any message once, or
# Telegram will not let it message you first.
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Timezone the alerts are rendered in. Europe/London matches what the official sites
# quote, so the message agrees with the page you click through to.
NOTIFIER_TZ=Europe/London

# How often the loop wakes, and how often it refetches both APIs. The defaults cost
# 48 requests a day and notice a moved deadline well before any alert for it.
NOTIFIER_POLL_SECONDS=60
NOTIFIER_REFRESH_SECONDS=3600

NOTIFIER_STATE_PATH=data/notifier_state.json
```

- [ ] **Step 11: Confirm the whole suite is green and the install still resolves**

```bash
.venv/bin/pytest -q
.venv/bin/python -c "import notifier.config; print(notifier.config.load_config({'TELEGRAM_BOT_TOKEN': 't', 'TELEGRAM_CHAT_ID': 'c'}))"
```

Expected: the existing suite passes unchanged, plus 12 new tests; the second command prints a `Config`.

- [ ] **Step 12: Commit**

```bash
git add notifier/ tests/notifier/ pyproject.toml .gitignore .env.example
git commit -m "feat(notifier): scaffold the package and its configuration

Config reads the environment when called rather than at import. app/config.py
does the opposite, and tests/conftest.py documents what that cost: a developer's
.env survived monkeypatch and left the suite one unpatched client away from a
real billable call. A plain dict argument makes the notifier's tests immune.

A test asserts no module under notifier/ imports from app/, so the deliberate
duplication in the spec cannot be quietly undone by a later refactor."
```

---

### Task 2: Deadline sources

**Files:**
- Create: `notifier/sources.py`
- Create: `tests/notifier/fixtures/fpl_bootstrap.json`
- Create: `tests/notifier/fixtures/draft_bootstrap.json`
- Create: `tests/notifier/test_sources.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `notifier.sources.Moment` — frozen dataclass, fields `kind: str`, `gw: int`, `when: datetime`, `games: frozenset[str]`.
  - `notifier.sources.FPL_URL` / `DRAFT_URL` — `str` constants.
  - `notifier.sources.parse_fpl(payload: dict) -> list[Moment]`
  - `notifier.sources.parse_draft(payload: dict) -> list[Moment]`
  - `notifier.sources.fetch_source(client: httpx.Client, game: str) -> list[Moment]` — `game` is `"fpl"` or `"draft"`; raises `httpx.HTTPError` on failure.
  - `notifier.sources.merge(*groups: Iterable[Moment]) -> list[Moment]` — sorted by `when`.

- [ ] **Step 1: Write the fixtures**

Create `tests/notifier/fixtures/fpl_bootstrap.json` — a trimmed capture of the live response from
2026-08-22, keeping the three fields the parser reads plus `name`/`finished` for realism. GW9 is
included because its deadline falls after the October clock change and Task 4 needs a real GMT
case:

```json
{
  "events": [
    {"id": 1, "name": "Gameweek 1", "deadline_time": "2026-08-21T17:30:00Z", "finished": false},
    {"id": 2, "name": "Gameweek 2", "deadline_time": "2026-08-28T17:30:00Z", "finished": false},
    {"id": 3, "name": "Gameweek 3", "deadline_time": "2026-09-04T17:30:00Z", "finished": false},
    {"id": 9, "name": "Gameweek 9", "deadline_time": "2026-10-31T11:00:00Z", "finished": false}
  ]
}
```

Create `tests/notifier/fixtures/draft_bootstrap.json` — note `events` is a dict here, not a list:

```json
{
  "events": {
    "current": 1,
    "next": 2,
    "data": [
      {"id": 1, "name": "Gameweek 1", "deadline_time": "2026-08-21T17:30:00Z", "waivers_time": "2026-08-20T17:30:00Z", "trades_time": "2026-08-19T17:30:00Z", "finished": false},
      {"id": 2, "name": "Gameweek 2", "deadline_time": "2026-08-28T17:30:00Z", "waivers_time": "2026-08-27T17:30:00Z", "trades_time": "2026-08-26T17:30:00Z", "finished": false},
      {"id": 3, "name": "Gameweek 3", "deadline_time": "2026-09-04T17:30:00Z", "waivers_time": "2026-09-03T17:30:00Z", "trades_time": "2026-09-02T17:30:00Z", "finished": false},
      {"id": 9, "name": "Gameweek 9", "deadline_time": "2026-10-31T11:00:00Z", "waivers_time": "2026-10-30T11:00:00Z", "trades_time": "2026-10-29T11:00:00Z", "finished": false}
    ]
  }
}
```

- [ ] **Step 2: Write the failing source tests**

Create `tests/notifier/test_sources.py`:

```python
import datetime
import json
import pathlib

import httpx
import pytest
import respx

from notifier.sources import (
    DRAFT_URL,
    FPL_URL,
    Moment,
    fetch_source,
    merge,
    parse_draft,
    parse_fpl,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
UTC = datetime.UTC


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _at(text):
    return datetime.datetime.fromisoformat(text)


def test_parse_fpl_reads_the_events_list():
    moments = parse_fpl(_fixture("fpl_bootstrap"))
    assert [m.gw for m in moments] == [1, 2, 3, 9]
    assert all(m.kind == "deadline" for m in moments)
    assert all(m.games == frozenset({"fpl"}) for m in moments)


def test_parse_fpl_produces_utc_aware_datetimes():
    """The API sends a trailing Z, which datetime.fromisoformat accepts on 3.13 but
    which produced naive datetimes on older versions. Everything downstream compares
    these against an aware `now`, and mixing the two raises TypeError at the worst
    possible moment."""
    moments = parse_fpl(_fixture("fpl_bootstrap"))
    gw2 = next(m for m in moments if m.gw == 2)
    assert gw2.when == _at("2026-08-28T17:30:00+00:00")
    assert gw2.when.tzinfo is not None
    assert gw2.when.utcoffset() == datetime.timedelta(0)


def test_parse_draft_reads_the_nested_events_data_list():
    """Classic returns `events` as a list; Draft returns a dict with current/next/data.
    Parsing Draft as though it were classic raises TypeError, so this is the one shape
    difference that has to be encoded rather than shared."""
    moments = parse_draft(_fixture("draft_bootstrap"))
    assert {m.gw for m in moments} == {1, 2, 3, 9}


def test_parse_draft_emits_both_a_deadline_and_a_waiver_moment_per_gameweek():
    moments = parse_draft(_fixture("draft_bootstrap"))
    gw2 = [m for m in moments if m.gw == 2]
    kinds = {m.kind: m.when for m in gw2}
    assert kinds == {
        "deadline": _at("2026-08-28T17:30:00+00:00"),
        "waivers": _at("2026-08-27T17:30:00+00:00"),
    }
    assert all(m.games == frozenset({"draft"}) for m in gw2)


def test_parse_draft_ignores_trades_time():
    """Out of scope per the spec. The field is right there in the payload, so this test
    is what stops a future reader adding it because it looked like an oversight."""
    moments = parse_draft(_fixture("draft_bootstrap"))
    assert {m.kind for m in moments} == {"deadline", "waivers"}


def test_parse_skips_an_event_with_a_null_time():
    """Observed on the Draft payload for gameweeks whose waiver window is not yet
    scheduled. A None here would crash fromisoformat during a refresh and take out an
    unrelated pending alert."""
    payload = {"events": {"data": [
        {"id": 5, "deadline_time": "2026-09-18T17:30:00Z", "waivers_time": None},
    ]}}
    moments = parse_draft(payload)
    assert [(m.kind, m.gw) for m in moments] == [("deadline", 5)]


def test_merge_unions_the_games_of_identical_moments():
    """The live check on 2026-08-22: both games put GW2 at 17:30Z. One merged moment
    means one alert instead of two saying the same thing a second apart."""
    merged = merge(parse_fpl(_fixture("fpl_bootstrap")), parse_draft(_fixture("draft_bootstrap")))
    gw2_deadline = [m for m in merged if m.gw == 2 and m.kind == "deadline"]
    assert len(gw2_deadline) == 1
    assert gw2_deadline[0].games == frozenset({"fpl", "draft"})


def test_merge_keeps_moments_that_differ_by_one_minute_apart():
    """The games have diverged before. An exact-equality merge must not round them
    together, or a genuinely earlier deadline would be announced at the later time."""
    fpl = Moment("deadline", 2, _at("2026-08-28T17:30:00+00:00"), frozenset({"fpl"}))
    draft = Moment("deadline", 2, _at("2026-08-28T17:31:00+00:00"), frozenset({"draft"}))
    merged = merge([fpl], [draft])
    assert len(merged) == 2
    assert [m.games for m in merged] == [frozenset({"fpl"}), frozenset({"draft"})]


def test_merge_keeps_a_waiver_and_a_deadline_at_the_same_instant_separate():
    """Same instant, different kind. Merging on time alone would collapse a waiver
    warning into a deadline warning and lose one of them."""
    a = Moment("deadline", 2, _at("2026-08-28T17:30:00+00:00"), frozenset({"fpl"}))
    b = Moment("waivers", 3, _at("2026-08-28T17:30:00+00:00"), frozenset({"draft"}))
    assert len(merge([a], [b])) == 2


def test_merge_returns_moments_sorted_by_time():
    merged = merge(parse_fpl(_fixture("fpl_bootstrap")), parse_draft(_fixture("draft_bootstrap")))
    assert merged == sorted(merged, key=lambda m: m.when)


def test_merge_of_nothing_is_empty():
    assert merge() == []
    assert merge([], []) == []


@respx.mock
def test_fetch_source_fpl_calls_the_classic_endpoint():
    route = respx.get(FPL_URL).mock(return_value=httpx.Response(200, json=_fixture("fpl_bootstrap")))
    with httpx.Client() as client:
        moments = fetch_source(client, "fpl")
    assert route.called
    assert len(moments) == 4


@respx.mock
def test_fetch_source_draft_calls_the_draft_endpoint():
    route = respx.get(DRAFT_URL).mock(return_value=httpx.Response(200, json=_fixture("draft_bootstrap")))
    with httpx.Client() as client:
        moments = fetch_source(client, "draft")
    assert route.called
    assert len(moments) == 8


@respx.mock
def test_fetch_source_raises_on_a_server_error():
    """The caller's job is to fall back to cache, which it can only do if this raises
    rather than returning an empty list. An empty list would read as "no deadlines
    exist" and silently retire every pending alert."""
    respx.get(FPL_URL).mock(return_value=httpx.Response(503))
    with httpx.Client() as client, pytest.raises(httpx.HTTPError):
        fetch_source(client, "fpl")


@respx.mock
def test_fetch_source_raises_on_a_connection_failure():
    respx.get(FPL_URL).mock(side_effect=httpx.ConnectError("no route"))
    with httpx.Client() as client, pytest.raises(httpx.HTTPError):
        fetch_source(client, "fpl")


def test_fetch_source_rejects_an_unknown_game():
    with httpx.Client() as client, pytest.raises(ValueError):
        fetch_source(client, "nonsense")


def test_moment_is_hashable_and_frozen():
    """State serialisation and the merge both put these in sets."""
    m = Moment("deadline", 1, _at("2026-08-21T17:30:00+00:00"), frozenset({"fpl"}))
    assert {m, m} == {m}
    with pytest.raises(Exception):
        m.gw = 2  # type: ignore[misc]
```

- [ ] **Step 3: Run the source tests to verify they fail**

Run: `.venv/bin/pytest tests/notifier/test_sources.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'notifier.sources'`

- [ ] **Step 4: Write `notifier/sources.py`**

```python
"""The two official endpoints, normalised to `Moment`.

Both were called unauthenticated on 2026-08-22 and both answered. Deadlines are
properties of the game rather than of an entry, so there is no team id, no league id
and no cookie to keep alive -- which is the whole reason the Draft waiver alert is
cheap enough to be worth having.
"""

import dataclasses
import datetime
from collections.abc import Iterable

import httpx

FPL_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
DRAFT_URL = "https://draft.premierleague.com/api/bootstrap-static"

TIMEOUT = 15.0


@dataclasses.dataclass(frozen=True)
class Moment:
    """A single instant something is due, in UTC.

    `games` is a set rather than a string because the two games publish the same
    gameweek deadline and a merged moment carries both.
    """

    kind: str  # "deadline" | "waivers"
    gw: int
    when: datetime.datetime  # timezone-aware, UTC
    games: frozenset[str]


def _parse_time(raw: str | None) -> datetime.datetime | None:
    # Draft leaves waivers_time null for gameweeks whose window is not scheduled yet.
    # Crashing on one of those during a refresh would take out unrelated pending alerts.
    if not raw:
        return None
    return datetime.datetime.fromisoformat(raw).astimezone(datetime.UTC)


def parse_fpl(payload: dict) -> list[Moment]:
    moments = []
    for event in payload.get("events", []):
        when = _parse_time(event.get("deadline_time"))
        if when is not None:
            moments.append(Moment("deadline", event["id"], when, frozenset({"fpl"})))
    return moments


def parse_draft(payload: dict) -> list[Moment]:
    # Draft nests the list one level deeper than classic does: `events` is a dict of
    # current/next/data rather than the list itself. Reusing parse_fpl here raises
    # TypeError, which is why the two functions exist separately.
    moments = []
    for event in payload.get("events", {}).get("data", []):
        for kind, field in (("deadline", "deadline_time"), ("waivers", "waivers_time")):
            when = _parse_time(event.get(field))
            if when is not None:
                moments.append(Moment(kind, event["id"], when, frozenset({"draft"})))
    return moments


_SOURCES = {"fpl": (FPL_URL, parse_fpl), "draft": (DRAFT_URL, parse_draft)}


def fetch_source(client: httpx.Client, game: str) -> list[Moment]:
    """Fetch and parse one game's deadlines. Raises httpx.HTTPError on any failure.

    Raising rather than returning [] is load-bearing: the caller falls back to its
    cached copy, and an empty list would instead read as "this game has no deadlines"
    and quietly retire every alert still pending for it.
    """
    if game not in _SOURCES:
        raise ValueError(f"unknown game {game!r}; expected one of {sorted(_SOURCES)}")
    url, parse = _SOURCES[game]
    response = client.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return parse(response.json())


def merge(*groups: Iterable[Moment]) -> list[Moment]:
    """Combine per-source moments, unioning `games` where kind, gw and instant match.

    Equality is exact. The two games have published different deadlines for the same
    gameweek before, and rounding them together would announce the earlier one at the
    later one's time -- the exact failure this service exists to prevent.
    """
    by_identity: dict[tuple[str, int, datetime.datetime], set[str]] = {}
    for group in groups:
        for moment in group:
            key = (moment.kind, moment.gw, moment.when)
            by_identity.setdefault(key, set()).update(moment.games)
    moments = [
        Moment(kind, gw, when, frozenset(games))
        for (kind, gw, when), games in by_identity.items()
    ]
    return sorted(moments, key=lambda m: (m.when, m.kind, m.gw))
```

- [ ] **Step 5: Run the source tests to verify they pass**

Run: `.venv/bin/pytest tests/notifier/test_sources.py -v`
Expected: PASS, 17 tests.

- [ ] **Step 6: Check the fixtures still match the live APIs**

The fixtures are captures, and a capture can drift. Confirm the shape assumptions hold today:

```bash
.venv/bin/python -c "
import httpx, notifier.sources as s
with httpx.Client() as c:
    print('fpl  ', len(s.fetch_source(c, 'fpl')), 'moments')
    print('draft', len(s.fetch_source(c, 'draft')), 'moments')
"
```

Expected: both print a non-zero count (38 and 76 for a full season). If either raises, the payload
shape has changed and the parser — not the test — needs updating.

- [ ] **Step 7: Commit**

```bash
git add notifier/sources.py tests/notifier/test_sources.py tests/notifier/fixtures/
git commit -m "feat(notifier): read deadlines from both official APIs

Classic returns events as a list, Draft as a dict of current/next/data, so
the two parsers stay separate rather than sharing a walker that would need a
shape check anyway.

merge unions games on an exact match of kind, gameweek and instant. Both games
put GW2 at 17:30Z today, so in practice a gameweek yields one moment and one
alert; equality is exact because the games have diverged before and rounding
them together would announce the earlier deadline at the later one's time.

fetch_source raises rather than returning [] on failure. An empty list would
read as 'this game has no deadlines' and retire every pending alert for it."
```

---

### Task 3: The schedule

The pure core. Every rule about which alerts fire and which are too stale to bother with lives here, and none of these tests touch a clock, a socket or the disk.

**Files:**
- Create: `notifier/schedule.py`
- Create: `tests/notifier/test_schedule.py`

**Interfaces:**
- Consumes: `notifier.sources.Moment`.
- Produces:
  - `notifier.schedule.OFFSETS_HOURS` — `tuple[int, ...]`, exactly `(24, 2)`.
  - `notifier.schedule.Alert` — frozen dataclass, fields `moment: Moment`, `offset_hours: int`; property `key -> str` returning `f"{moment.kind}:{moment.gw}:{offset_hours}"`; property `trigger -> datetime` returning `moment.when - timedelta(hours=offset_hours)`.
  - `notifier.schedule.due_alerts(moments: Iterable[Moment], sent: Set[str], now: datetime) -> tuple[list[Alert], list[Alert]]` — returns `(to_send, to_retire)`, both sorted by trigger time.

- [ ] **Step 1: Write the failing schedule tests**

Create `tests/notifier/test_schedule.py`:

```python
import datetime

import pytest

from notifier.schedule import OFFSETS_HOURS, Alert, due_alerts
from notifier.sources import Moment

DEADLINE = datetime.datetime(2026, 8, 28, 17, 30, tzinfo=datetime.UTC)
WAIVERS = datetime.datetime(2026, 8, 27, 17, 30, tzinfo=datetime.UTC)

GW2_DEADLINE = Moment("deadline", 2, DEADLINE, frozenset({"fpl", "draft"}))
GW2_WAIVERS = Moment("waivers", 2, WAIVERS, frozenset({"draft"}))
BOTH = [GW2_DEADLINE, GW2_WAIVERS]


def at(**delta):
    """A `now` expressed relative to the GW2 deadline, e.g. at(hours=-2)."""
    return DEADLINE + datetime.timedelta(**delta)


def keys(alerts):
    return [a.key for a in alerts]


def test_the_offsets_are_exactly_one_day_and_two_hours():
    assert OFFSETS_HOURS == (24, 2)


def test_alert_key_format():
    assert Alert(GW2_WAIVERS, 24).key == "waivers:2:24"
    assert Alert(GW2_DEADLINE, 2).key == "deadline:2:2"


def test_alert_trigger_is_the_offset_before_the_moment():
    assert Alert(GW2_DEADLINE, 2).trigger == at(hours=-2)


def test_nothing_is_due_long_before_the_first_trigger():
    to_send, to_retire = due_alerts(BOTH, set(), at(days=-7))
    assert to_send == []
    assert to_retire == []


def test_the_two_hour_alert_fires_exactly_on_its_trigger():
    """A tick landing precisely on the boundary must send, not wait for the next one.
    With a 60s poll a strict `>` would be right 59 times out of 60 and look correct in
    every hand test."""
    to_send, _ = due_alerts([GW2_DEADLINE], set(), at(hours=-2))
    assert keys(to_send) == ["deadline:2:2"]


def test_a_tick_a_second_before_the_trigger_sends_nothing():
    to_send, _ = due_alerts([GW2_DEADLINE], set(), at(hours=-2, seconds=-1))
    assert to_send == []


def test_the_four_alerts_of_a_gameweek_fall_at_four_distinct_times():
    """The spec's timetable: D-48, D-26, D-24, D-2. Nothing collides on an unmoved
    schedule, so the merge path below is defensive rather than routine."""
    triggers = {Alert(m, o).trigger for m in BOTH for o in OFFSETS_HOURS}
    assert len(triggers) == 4
    assert sorted(triggers) == [at(hours=-48), at(hours=-26), at(hours=-24), at(hours=-2)]


def test_the_deadline_day_alert_fires_when_the_waiver_window_shuts():
    """Draft's waivers_before_deadline_hours is 24, so these coincide by construction.
    One is a warning about tomorrow, the other is about right now, and both are wanted."""
    assert Alert(GW2_DEADLINE, 24).trigger == GW2_WAIVERS.when


def test_a_moved_deadline_can_put_two_alerts_in_one_tick():
    """FPL reschedules deadlines. This is the case the list-taking renderer exists for."""
    moved = Moment("deadline", 3, WAIVERS + datetime.timedelta(hours=2), frozenset({"fpl"}))
    to_send, _ = due_alerts([GW2_WAIVERS, moved], set(), at(hours=-24))
    assert sorted(keys(to_send)) == ["deadline:3:2", "waivers:2:2"]


def test_an_already_sent_key_is_not_resent():
    to_send, to_retire = due_alerts([GW2_DEADLINE], {"deadline:2:2"}, at(hours=-2))
    assert to_send == []
    assert to_retire == []


def test_both_offsets_of_one_moment_are_independent():
    """Sending the 24h alert must not suppress the 2h one."""
    to_send, _ = due_alerts([GW2_DEADLINE], {"deadline:2:24"}, at(hours=-2))
    assert keys(to_send) == ["deadline:2:2"]


def test_a_late_alert_still_sends_while_its_moment_is_ahead():
    """Down for three hours across the 24h trigger, back up with 21 hours to spare. The
    alert is late but still actionable, so it goes."""
    to_send, to_retire = due_alerts([GW2_DEADLINE], set(), at(hours=-21))
    assert keys(to_send) == ["deadline:2:24"]
    assert to_retire == []


def test_an_alert_whose_moment_has_passed_is_retired_not_sent():
    """Down for a week. Warning about a deadline that has already gone is noise, and a
    long outage would otherwise deliver a burst of it on restart."""
    to_send, to_retire = due_alerts(BOTH, set(), at(hours=1))
    assert to_send == []
    assert sorted(keys(to_retire)) == [
        "deadline:2:2", "deadline:2:24", "waivers:2:2", "waivers:2:24",
    ]


def test_a_retired_alert_is_not_offered_twice():
    """Retirement is only useful if the caller writes the keys; given that, a second
    restart must find nothing left to do."""
    _, to_retire = due_alerts(BOTH, set(), at(hours=1))
    _, again = due_alerts(BOTH, {a.key for a in to_retire}, at(hours=1))
    assert again == []


def test_a_moment_exactly_now_is_retired_rather_than_sent():
    """Zero notice is not a warning."""
    to_send, to_retire = due_alerts([GW2_DEADLINE], set(), DEADLINE)
    assert to_send == []
    assert keys(to_retire) == ["deadline:2:24"]


def test_one_gameweek_passing_does_not_retire_the_next():
    later = Moment("deadline", 3, DEADLINE + datetime.timedelta(days=7), frozenset({"fpl"}))
    to_send, to_retire = due_alerts([GW2_DEADLINE, later], set(), at(hours=1))
    assert keys(to_send) == []
    assert all(a.moment.gw == 2 for a in to_retire)


def test_results_are_sorted_by_trigger_time():
    moved = Moment("deadline", 3, WAIVERS + datetime.timedelta(minutes=30), frozenset({"fpl"}))
    to_send, _ = due_alerts([GW2_WAIVERS, moved], set(), at(hours=-23))
    assert [a.trigger for a in to_send] == sorted(a.trigger for a in to_send)


def test_a_naive_now_is_rejected():
    """Comparing a naive datetime against an aware one raises TypeError deep inside the
    comparison. Failing here names the actual problem."""
    with pytest.raises(TypeError):
        due_alerts(BOTH, set(), datetime.datetime(2026, 8, 28, 17, 30))


def test_no_moments_is_not_an_error():
    assert due_alerts([], set(), at(hours=-2)) == ([], [])
```

- [ ] **Step 2: Run the schedule tests to verify they fail**

Run: `.venv/bin/pytest tests/notifier/test_schedule.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'notifier.schedule'`

- [ ] **Step 3: Write `notifier/schedule.py`**

```python
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
    needs a message. Retiring is what stops a restart after a long outage from
    delivering a burst of warnings about deadlines that have already gone.
    """
    if now.tzinfo is None:
        raise TypeError("due_alerts needs a timezone-aware `now`; got a naive datetime")

    to_send: list[Alert] = []
    to_retire: list[Alert] = []
    for moment in moments:
        for offset in OFFSETS_HOURS:
            alert = Alert(moment, offset)
            if alert.key in sent or alert.trigger > now:
                continue
            # `>=` rather than `>`: a moment landing exactly on this tick offers zero
            # notice, which is not a warning.
            if moment.when <= now:
                to_retire.append(alert)
            else:
                to_send.append(alert)
    key = lambda alert: (alert.trigger, alert.key)  # noqa: E731
    return sorted(to_send, key=key), sorted(to_retire, key=key)
```

- [ ] **Step 4: Run the schedule tests to verify they pass**

Run: `.venv/bin/pytest tests/notifier/test_schedule.py -v`
Expected: PASS, 19 tests.

- [ ] **Step 5: Commit**

```bash
git add notifier/schedule.py tests/notifier/test_schedule.py
git commit -m "feat(notifier): decide which alerts are owed

due_alerts takes `now` as an argument, so the boundaries are testable at an
exact instant rather than approximately. The trigger comparison is >=: with a
60s poll, a strict > would be right 59 ticks out of 60 and look correct in
every hand test.

Alerts split into send and retire. Retiring is what stops a restart after a
long outage from delivering a burst of warnings about deadlines already gone --
the caller writes both sets to state, but only sends the first.

Keys are per gameweek, not per timestamp, so a rescheduled deadline is not
mistaken for a new alert."
```

---

### Task 4: Rendering the message

**Files:**
- Create: `notifier/render.py`
- Create: `tests/notifier/test_render.py`

**Interfaces:**
- Consumes: `notifier.schedule.Alert`, `notifier.sources.Moment`.
- Produces: `notifier.render.format_message(alerts: Sequence[Alert], tz: ZoneInfo) -> str`.

Note: the spec's architecture listing gained `render.py` as a separate module during planning. Formatting is pure and has one responsibility, and keeping it out of `schedule.py` means the scheduling rules can be read without scrolling past copy.

- [ ] **Step 1: Write the failing render tests**

Create `tests/notifier/test_render.py`:

```python
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
```

- [ ] **Step 2: Run the render tests to verify they fail**

Run: `.venv/bin/pytest tests/notifier/test_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'notifier.render'`

- [ ] **Step 3: Write `notifier/render.py`**

```python
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
```

- [ ] **Step 4: Run the render tests to verify they pass**

Run: `.venv/bin/pytest tests/notifier/test_render.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Commit**

```bash
git add notifier/render.py tests/notifier/test_render.py
git commit -m "feat(notifier): render alerts as Telegram message text

The only place a UTC instant becomes a local one, so the only place a
clock-change bug can live. A test pins GW9 -- 31 October, after the change --
to GMT, because a hardcoded offset would pass all summer and be an hour wrong
from November.

Games render in a fixed order: frozenset iteration is not stable across runs,
and a message alternating between 'FPL + Draft' and 'Draft + FPL' reads as a
bug. Waivers say 'waiver window closes' rather than 'deadline', which would be
ambiguous when Draft has two."
```

---

### Task 5: Persistent state

**Files:**
- Create: `notifier/state.py`
- Create: `tests/notifier/test_state.py`

**Interfaces:**
- Consumes: `notifier.sources.Moment`.
- Produces:
  - `notifier.state.State` — mutable dataclass, fields `sent: set[str]`, `cached: dict[str, list[Moment]]`.
  - `notifier.state.load_state(path: Path) -> State`
  - `notifier.state.save_state(path: Path, state: State) -> None`

- [ ] **Step 1: Write the failing state tests**

Create `tests/notifier/test_state.py`:

```python
import datetime
import json

from notifier.sources import Moment
from notifier.state import State, load_state, save_state

WHEN = datetime.datetime(2026, 8, 28, 17, 30, tzinfo=datetime.UTC)
MOMENT = Moment("deadline", 2, WHEN, frozenset({"fpl", "draft"}))


def test_a_saved_state_round_trips(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, State(sent={"deadline:2:24"}, cached={"fpl": [MOMENT]}))
    restored = load_state(path)
    assert restored.sent == {"deadline:2:24"}
    assert restored.cached == {"fpl": [MOMENT]}


def test_a_restored_moment_keeps_its_utc_awareness(tmp_path):
    """A naive datetime out of the cache would raise TypeError inside due_alerts on the
    first tick after a restart -- exactly when nobody is watching."""
    path = tmp_path / "state.json"
    save_state(path, State(sent=set(), cached={"fpl": [MOMENT]}))
    restored = load_state(path).cached["fpl"][0]
    assert restored.when.tzinfo is not None
    assert restored.when == WHEN


def test_a_restored_moment_keeps_games_as_a_frozenset(tmp_path):
    """JSON has no set type, so this survives a list round trip only if it is rebuilt."""
    path = tmp_path / "state.json"
    save_state(path, State(sent=set(), cached={"fpl": [MOMENT]}))
    assert load_state(path).cached["fpl"][0].games == frozenset({"fpl", "draft"})


def test_a_missing_file_loads_as_empty(tmp_path):
    state = load_state(tmp_path / "absent.json")
    assert state.sent == set()
    assert state.cached == {}


def test_a_corrupt_file_loads_as_empty(tmp_path):
    """Half a JSON object, from a crash mid-write. Refusing to start here would mean a
    truncated file silently costs every future alert."""
    path = tmp_path / "state.json"
    path.write_text('{"sent": ["deadline:2:24"')
    state = load_state(path)
    assert state.sent == set()


def test_a_file_of_the_wrong_shape_loads_as_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('["not", "an", "object"]')
    assert load_state(path).sent == set()


def test_a_cached_entry_with_an_unreadable_moment_is_dropped_not_fatal(tmp_path):
    """Only that source's cache is lost; the other source and the sent keys survive, so
    a schema change costs a refetch rather than a duplicate alert storm."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "sent": ["deadline:2:24"],
        "cached": {"fpl": [{"kind": "deadline"}], "draft": []},
    }))
    state = load_state(path)
    assert state.sent == {"deadline:2:24"}
    assert "fpl" not in state.cached


def test_save_creates_the_parent_directory(tmp_path):
    """data/ is gitignored except for .gitkeep, so a fresh clone on the server can
    plausibly reach the first save without it existing."""
    path = tmp_path / "nested" / "deeper" / "state.json"
    save_state(path, State(sent={"a"}, cached={}))
    assert load_state(path).sent == {"a"}


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    """A crash mid-write must not truncate a good file. Written to a sibling temp and
    renamed, so a reader sees the old file or the new one."""
    path = tmp_path / "state.json"
    save_state(path, State(sent={"a"}, cached={"fpl": [MOMENT]}))
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_overwriting_replaces_rather_than_merges(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, State(sent={"old"}, cached={}))
    save_state(path, State(sent={"new"}, cached={}))
    assert load_state(path).sent == {"new"}


def test_the_file_is_human_readable(tmp_path):
    """It is the only window into why an alert did or did not fire."""
    path = tmp_path / "state.json"
    save_state(path, State(sent={"deadline:2:24"}, cached={"fpl": [MOMENT]}))
    text = path.read_text()
    assert "deadline:2:24" in text
    assert "\n" in text
```

- [ ] **Step 2: Run the state tests to verify they fail**

Run: `.venv/bin/pytest tests/notifier/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'notifier.state'`

- [ ] **Step 3: Write `notifier/state.py`**

```python
"""The notifier's memory: which alerts have been handled, and the last good deadlines.

One file for both, because they are written on the same tick and a half-restored pair
would be worse than either alone.

Every read path degrades to empty rather than raising. A notifier that refuses to start
because of a truncated JSON file has turned a recoverable problem into a missed
deadline, and the schedule's retirement rule already makes an empty state cheap: on the
next tick everything already past is retired silently instead of sent.
"""

import dataclasses
import datetime
import json
import pathlib

from notifier.sources import Moment


@dataclasses.dataclass
class State:
    sent: set[str]
    # Per source ("fpl" / "draft") rather than merged, so one API failing can fall back
    # to its own cached half while the other stays fresh. Splitting a merged list back
    # apart would mean guessing which game contributed which moment.
    cached: dict[str, list[Moment]]


def _moment_to_json(moment: Moment) -> dict:
    return {
        "kind": moment.kind,
        "gw": moment.gw,
        "when": moment.when.isoformat(),
        "games": sorted(moment.games),
    }


def _moment_from_json(raw: dict) -> Moment:
    when = datetime.datetime.fromisoformat(raw["when"])
    if when.tzinfo is None:
        # Would raise TypeError inside due_alerts on the first tick after a restart.
        raise ValueError(f"cached moment has a naive datetime: {raw['when']!r}")
    return Moment(raw["kind"], int(raw["gw"]), when, frozenset(raw["games"]))


def load_state(path: pathlib.Path) -> State:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return State(sent=set(), cached={})
    if not isinstance(raw, dict):
        return State(sent=set(), cached={})

    sent = set(raw.get("sent") or [])
    cached: dict[str, list[Moment]] = {}
    for game, entries in (raw.get("cached") or {}).items():
        try:
            cached[game] = [_moment_from_json(entry) for entry in entries]
        except (KeyError, TypeError, ValueError):
            # Drop this source's cache only. The other source and the sent keys are
            # still good, so a schema change costs a refetch, not a duplicate storm.
            continue
    return State(sent=sent, cached=cached)


def save_state(path: pathlib.Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sent": sorted(state.sent),
        "cached": {
            game: [_moment_to_json(m) for m in moments]
            for game, moments in state.cached.items()
        },
    }
    # Write and rename, so a crash mid-write leaves the previous file intact rather
    # than a truncated one. The temp sits in the same directory to keep the rename on
    # one filesystem, where it is atomic.
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    temp.replace(path)
```

- [ ] **Step 4: Run the state tests to verify they pass**

Run: `.venv/bin/pytest tests/notifier/test_state.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Commit**

```bash
git add notifier/state.py tests/notifier/test_state.py
git commit -m "feat(notifier): persist sent keys and the deadline cache

Every read path degrades to empty rather than raising. Refusing to start on a
truncated JSON file turns a recoverable problem into a missed deadline, and
the schedule's retirement rule already makes an empty state cheap.

Writes go to a sibling temp and are renamed, so a crash mid-write leaves the
previous file intact instead of truncating it.

The cache is per source, not merged: one API failing falls back to its own
cached half while the other stays fresh, and splitting a merged list apart
would mean guessing which game contributed what."
```

---

### Task 6: Sending to Telegram

**Files:**
- Create: `notifier/telegram.py`
- Create: `tests/notifier/test_telegram.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `notifier.telegram.TelegramError(Exception)`
  - `notifier.telegram.ATTEMPTS` — `int`, `3`.
  - `notifier.telegram.api_url(token: str) -> str`
  - `notifier.telegram.send_message(client: httpx.Client, token: str, chat_id: str, text: str, sleep: Callable[[float], None] = time.sleep) -> None` — raises `TelegramError` after all attempts fail.

- [ ] **Step 1: Write the failing telegram tests**

Create `tests/notifier/test_telegram.py`:

```python
import httpx
import pytest
import respx

from notifier.telegram import ATTEMPTS, TelegramError, api_url, send_message

TOKEN = "123456:AAtoken"
CHAT = "987"
URL = api_url(TOKEN)
OK = {"ok": True, "result": {"message_id": 1}}


def _recording_sleep():
    slept = []
    return slept, slept.append


@respx.mock
def test_a_successful_send_posts_the_chat_id_and_text():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=OK))
    with httpx.Client() as client:
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert route.call_count == 1
    assert route.calls[0].request.read() == b'{"chat_id": "987", "text": "hello"}'


def test_the_token_is_in_the_url_path_not_a_query_string():
    """A token in a query string lands in every proxy and server log it passes."""
    assert api_url(TOKEN) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"


@respx.mock
def test_a_transient_failure_is_retried_and_can_succeed():
    route = respx.post(URL).mock(side_effect=[
        httpx.Response(502),
        httpx.Response(200, json=OK),
    ])
    with httpx.Client() as client:
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert route.call_count == 2


@respx.mock
def test_a_connection_error_is_retried():
    """The likeliest real failure on a home server is the network, not Telegram."""
    route = respx.post(URL).mock(side_effect=[
        httpx.ConnectError("no route"),
        httpx.Response(200, json=OK),
    ])
    with httpx.Client() as client:
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert route.call_count == 2


@respx.mock
def test_it_gives_up_after_three_attempts():
    route = respx.post(URL).mock(return_value=httpx.Response(500))
    with httpx.Client() as client, pytest.raises(TelegramError):
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert route.call_count == ATTEMPTS == 3


@respx.mock
def test_the_backoff_grows_between_attempts_and_does_not_sleep_after_the_last():
    """Sleeping after the final failure delays the raise for no benefit, and the tick
    loop is the thing that should decide when to try again."""
    respx.post(URL).mock(return_value=httpx.Response(500))
    slept, sleep = _recording_sleep()
    with httpx.Client() as client, pytest.raises(TelegramError):
        send_message(client, TOKEN, CHAT, "hello", sleep=sleep)
    assert slept == [1.0, 4.0]


@respx.mock
def test_a_401_is_not_retried():
    """A bad token will be bad on the third attempt too. Failing fast puts the real
    cause in the log instead of three timeouts."""
    route = respx.post(URL).mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    with httpx.Client() as client, pytest.raises(TelegramError) as excinfo:
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert route.call_count == 1
    assert "Unauthorized" in str(excinfo.value)


@respx.mock
def test_a_400_is_not_retried():
    """Chat not found, or a chat that has never messaged the bot. Also permanent."""
    route = respx.post(URL).mock(
        return_value=httpx.Response(400, json={"ok": False, "description": "chat not found"})
    )
    with httpx.Client() as client, pytest.raises(TelegramError):
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert route.call_count == 1


@respx.mock
def test_a_429_is_retried():
    """Rate limiting is transient, unlike the other 4xx cases."""
    route = respx.post(URL).mock(side_effect=[
        httpx.Response(429), httpx.Response(200, json=OK),
    ])
    with httpx.Client() as client:
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert route.call_count == 2


@respx.mock
def test_the_bot_token_is_not_repeated_in_the_error_message():
    """The error is logged, and logs get pasted into issues."""
    respx.post(URL).mock(return_value=httpx.Response(500))
    with httpx.Client() as client, pytest.raises(TelegramError) as excinfo:
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert TOKEN not in str(excinfo.value)


@respx.mock
def test_an_ok_false_body_with_a_200_status_is_still_a_failure():
    """Telegram has answered 200 with ok:false. Treating that as sent would write the
    key and permanently swallow the alert."""
    respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": False, "description": "nope"}))
    with httpx.Client() as client, pytest.raises(TelegramError):
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
```

- [ ] **Step 2: Run the telegram tests to verify they fail**

Run: `.venv/bin/pytest tests/notifier/test_telegram.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'notifier.telegram'`

- [ ] **Step 3: Write `notifier/telegram.py`**

```python
"""One Bot API call: sendMessage, with retries.

No python-telegram-bot dependency. The service pushes and never reads, so it needs one
POST -- a library would bring an update loop, a dispatcher and a framework's worth of
lifecycle for a function that fits on a screen.
"""

import time
from collections.abc import Callable

import httpx

ATTEMPTS = 3
TIMEOUT = 15.0
# 1s, then 4s. No sleep after the final attempt: it would delay the raise for no
# benefit, and the tick loop already decides when to try again.
_BACKOFF = (1.0, 4.0)

# A bad token or an unknown chat will be just as bad on the third attempt. Failing fast
# puts the real cause in the log instead of three timeouts. 429 is excluded: rate
# limiting is transient.
_PERMANENT = {400, 401, 403, 404}


class TelegramError(Exception):
    """Raised when a message could not be delivered. State must not be written."""


def api_url(token: str) -> str:
    # Path, not query string: a token in a query string lands in every proxy log on
    # the way.
    return f"https://api.telegram.org/bot{token}/sendMessage"


def _describe(response: httpx.Response) -> str:
    try:
        return str(response.json().get("description", response.text))[:200]
    except ValueError:
        return response.text[:200]


def send_message(
    client: httpx.Client,
    token: str,
    chat_id: str,
    text: str,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Deliver one message, or raise TelegramError having tried ATTEMPTS times.

    `sleep` is injected so the retry tests do not spend five real seconds.
    """
    last = "no attempt made"
    for attempt in range(ATTEMPTS):
        try:
            response = client.post(
                api_url(token),
                json={"chat_id": chat_id, "text": text},
                timeout=TIMEOUT,
            )
        except httpx.HTTPError as exc:
            # The likeliest real failure on a home server is the network, not Telegram.
            last = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code in _PERMANENT:
                raise TelegramError(
                    f"Telegram rejected the message with {response.status_code}: "
                    f"{_describe(response)}"
                )
            if response.status_code == 200:
                body = response.json()
                if body.get("ok"):
                    return
                # Telegram has answered 200 with ok:false. Treating that as delivered
                # would write the sent key and swallow the alert for good.
                last = f"200 but ok=false: {_describe(response)}"
            else:
                last = f"HTTP {response.status_code}: {_describe(response)}"
        if attempt < len(_BACKOFF):
            sleep(_BACKOFF[attempt])
    # The token is deliberately absent from this message: it is logged, and logs get
    # pasted into issues.
    raise TelegramError(f"Telegram send failed after {ATTEMPTS} attempts. Last: {last}")
```

- [ ] **Step 4: Run the telegram tests to verify they pass**

Run: `.venv/bin/pytest tests/notifier/test_telegram.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Commit**

```bash
git add notifier/telegram.py tests/notifier/test_telegram.py
git commit -m "feat(notifier): send messages through the Bot API

One POST, no python-telegram-bot. The service pushes and never reads, so a
library would bring an update loop and a dispatcher for a function that fits
on a screen.

400/401/403/404 fail immediately -- a bad token is bad on the third attempt
too, and failing fast puts the real cause in the log rather than three
timeouts. 429 still retries, because rate limiting is transient.

A 200 carrying ok:false counts as a failure: Telegram does answer that way,
and treating it as delivered would write the sent key and swallow the alert."
```

---

### Task 7: The tick loop

**Files:**
- Create: `notifier/__main__.py`
- Create: `tests/notifier/test_main.py`

**Interfaces:**
- Consumes: everything from Tasks 1–6.
- Produces:
  - `notifier.__main__.run_once(cfg: Config, client: httpx.Client, state: State, now: datetime, refresh: bool) -> None` — mutates and saves `state`.
  - `notifier.__main__.main(argv: Sequence[str] | None = None) -> int`

- [ ] **Step 1: Write the failing loop tests**

Create `tests/notifier/test_main.py`:

```python
import datetime
import json
import pathlib
import zoneinfo

import httpx
import pytest
import respx

from notifier.__main__ import main, run_once
from notifier.config import Config
from notifier.sources import DRAFT_URL, FPL_URL, Moment
from notifier.state import State, load_state
from notifier.telegram import api_url

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
TOKEN = "123:abc"
SEND_URL = api_url(TOKEN)
OK = {"ok": True, "result": {"message_id": 1}}

DEADLINE = datetime.datetime(2026, 8, 28, 17, 30, tzinfo=datetime.UTC)


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _cfg(tmp_path):
    return Config(
        bot_token=TOKEN,
        chat_id="987",
        tz=zoneinfo.ZoneInfo("Europe/London"),
        poll_seconds=60.0,
        refresh_seconds=3600.0,
        state_path=tmp_path / "state.json",
    )


def _mock_apis():
    respx.get(FPL_URL).mock(return_value=httpx.Response(200, json=_fixture("fpl_bootstrap")))
    respx.get(DRAFT_URL).mock(return_value=httpx.Response(200, json=_fixture("draft_bootstrap")))


@respx.mock
def test_a_tick_two_hours_before_the_deadline_sends_one_merged_message(tmp_path):
    _mock_apis()
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    state = State(sent=set(), cached={})
    with httpx.Client() as client:
        run_once(_cfg(tmp_path), client, state, DEADLINE - datetime.timedelta(hours=2), refresh=True)
    assert send.call_count == 1
    body = json.loads(send.calls[0].request.read())
    assert body["text"] == "⏰ GW2 deadline in 2 hours\nFPL + Draft · Fri 28 Aug, 18:30 BST"


@respx.mock
def test_a_successful_send_is_persisted_so_the_next_tick_is_silent(tmp_path):
    _mock_apis()
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    state = State(sent=set(), cached={})
    now = DEADLINE - datetime.timedelta(hours=2)
    with httpx.Client() as client:
        run_once(cfg, client, state, now, refresh=True)
        run_once(cfg, client, state, now + datetime.timedelta(minutes=1), refresh=False)
    assert send.call_count == 1
    assert "deadline:2:2" in load_state(cfg.state_path).sent


@respx.mock
def test_a_failed_send_leaves_the_key_unwritten_so_the_next_tick_retries(tmp_path):
    """The single most important behaviour here: a Telegram outage must postpone the
    alert, never consume it."""
    _mock_apis()
    send = respx.post(SEND_URL).mock(side_effect=[
        httpx.Response(500), httpx.Response(500), httpx.Response(500),
        httpx.Response(200, json=OK),
    ])
    cfg = _cfg(tmp_path)
    state = State(sent=set(), cached={})
    now = DEADLINE - datetime.timedelta(hours=2)
    with httpx.Client() as client:
        run_once(cfg, client, state, now, refresh=True)
        assert load_state(cfg.state_path).sent == set()
        run_once(cfg, client, state, now + datetime.timedelta(minutes=1), refresh=False)
    assert send.call_count == 4
    assert "deadline:2:2" in load_state(cfg.state_path).sent


@respx.mock
def test_nothing_due_means_no_telegram_call_at_all(tmp_path):
    _mock_apis()
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    with httpx.Client() as client:
        run_once(_cfg(tmp_path), client, State(sent=set(), cached={}),
                 DEADLINE - datetime.timedelta(days=30), refresh=True)
    assert send.call_count == 0


@respx.mock
def test_a_refresh_caches_both_sources_to_disk(tmp_path):
    _mock_apis()
    cfg = _cfg(tmp_path)
    state = State(sent=set(), cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, DEADLINE - datetime.timedelta(days=30), refresh=True)
    cached = load_state(cfg.state_path).cached
    assert set(cached) == {"fpl", "draft"}
    assert cached["fpl"] and cached["draft"]


@respx.mock
def test_refresh_false_makes_no_api_calls(tmp_path):
    """The loop refetches hourly, not every minute. 48 requests a day, not 2880."""
    fpl = respx.get(FPL_URL).mock(return_value=httpx.Response(200, json=_fixture("fpl_bootstrap")))
    respx.get(DRAFT_URL).mock(return_value=httpx.Response(200, json=_fixture("draft_bootstrap")))
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    state = State(sent=set(), cached={"fpl": [Moment("deadline", 2, DEADLINE, frozenset({"fpl"}))]})
    with httpx.Client() as client:
        run_once(_cfg(tmp_path), client, state, DEADLINE - datetime.timedelta(hours=2), refresh=False)
    assert fpl.call_count == 0


@respx.mock
def test_one_source_failing_still_sends_the_other_from_cache(tmp_path):
    """A Draft outage must not suppress an FPL deadline, and the reverse."""
    respx.get(FPL_URL).mock(return_value=httpx.Response(200, json=_fixture("fpl_bootstrap")))
    respx.get(DRAFT_URL).mock(return_value=httpx.Response(503))
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    state = State(sent=set(), cached={"draft": [Moment("waivers", 2, DEADLINE, frozenset({"draft"}))]})
    with httpx.Client() as client:
        run_once(_cfg(tmp_path), client, state, DEADLINE - datetime.timedelta(hours=2), refresh=True)
    body = json.loads(send.calls[0].request.read())
    assert "FPL + Draft" in body["text"] or "waiver" in body["text"]
    assert state.cached["draft"]  # the stale half survived the failed refresh


@respx.mock
def test_both_sources_failing_with_no_cache_is_not_fatal(tmp_path):
    """First run on a box with no network. Nothing to send, nothing lost, no crash."""
    respx.get(FPL_URL).mock(return_value=httpx.Response(503))
    respx.get(DRAFT_URL).mock(side_effect=httpx.ConnectError("no route"))
    with httpx.Client() as client:
        run_once(_cfg(tmp_path), client, State(sent=set(), cached={}),
                 DEADLINE - datetime.timedelta(hours=2), refresh=True)


@respx.mock
def test_a_restart_after_a_long_outage_retires_stale_alerts_silently(tmp_path):
    """Down for a week, back up after the deadline. Zero messages, and the keys are
    written so a second restart finds nothing left."""
    _mock_apis()
    send = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    cfg = _cfg(tmp_path)
    state = State(sent=set(), cached={})
    with httpx.Client() as client:
        run_once(cfg, client, state, DEADLINE + datetime.timedelta(hours=1), refresh=True)
    assert send.call_count == 0
    assert "deadline:2:2" in load_state(cfg.state_path).sent


def test_main_exits_nonzero_with_a_named_variable_when_config_is_missing(monkeypatch, capsys):
    """A notifier that starts and never sends is worse than one that refuses to start."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert main([]) == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


@respx.mock
def test_main_once_runs_a_single_tick_and_exits_zero(monkeypatch, tmp_path):
    """--once is what the systemd unit's ExecStartPre and a manual smoke test use."""
    _mock_apis()
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=OK))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987")
    monkeypatch.setenv("NOTIFIER_STATE_PATH", str(tmp_path / "state.json"))
    assert main(["--once"]) == 0
    assert (tmp_path / "state.json").exists()
```

- [ ] **Step 2: Run the loop tests to verify they fail**

Run: `.venv/bin/pytest tests/notifier/test_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'notifier.__main__'`

- [ ] **Step 3: Write `notifier/__main__.py`**

```python
"""The tick loop.

Wakes on a fixed interval, refetches on a slower one, and asks the pure scheduler what
is owed. Everything decidable lives in schedule.py and render.py; this module only
wires the impure edges together, which is why it has one test per wiring decision
rather than a test per rule.
"""

import argparse
import datetime
import logging
import signal
import sys
import time
from collections.abc import Sequence

import httpx

from notifier import sources
from notifier.config import Config, ConfigError, load_config
from notifier.render import format_message
from notifier.schedule import due_alerts
from notifier.state import State, load_state, save_state
from notifier.telegram import TelegramError, send_message

log = logging.getLogger("notifier")

GAMES = ("fpl", "draft")


def _refresh(client: httpx.Client, state: State) -> None:
    """Refetch each source, keeping the previous copy of anything that fails.

    Per source rather than all-or-nothing: a Draft outage must not suppress an FPL
    deadline. A source that fails with nothing cached simply contributes nothing, which
    is correct -- there is no such thing as a missed alert for a deadline never seen.
    """
    for game in GAMES:
        try:
            state.cached[game] = sources.fetch_source(client, game)
        except httpx.HTTPError as exc:
            held = len(state.cached.get(game, []))
            log.warning("refresh failed for %s (%s); holding %d cached moments", game, exc, held)


def run_once(
    cfg: Config,
    client: httpx.Client,
    state: State,
    now: datetime.datetime,
    refresh: bool,
) -> None:
    """One tick: maybe refetch, work out what is owed, send it, record it."""
    if refresh:
        _refresh(client, state)

    moments = sources.merge(*state.cached.values())
    to_send, to_retire = due_alerts(moments, state.sent, now)

    for alert in to_retire:
        log.info("retiring %s: its moment has passed", alert.key)
    # Retirements are recorded whether or not a send follows, so a restart after a long
    # outage settles in one tick instead of re-evaluating every time.
    state.sent.update(alert.key for alert in to_retire)

    if to_send:
        try:
            send_message(client, cfg.bot_token, cfg.chat_id, format_message(to_send, cfg.tz))
        except TelegramError as exc:
            # Deliberately not recorded. The next tick retries, and keeps retrying
            # until it succeeds or the moment passes and retirement takes over.
            log.error("send failed, will retry next tick: %s", exc)
        else:
            log.info("sent %s", ", ".join(a.key for a in to_send))
            state.sent.update(alert.key for alert in to_send)

    if to_send or to_retire or refresh:
        save_state(cfg.state_path, state)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="notifier", description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one tick and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    try:
        cfg = load_config()
    except ConfigError as exc:
        # Loud and named. A notifier that starts and silently never sends is worse than
        # one that refuses to start.
        print(f"notifier: {exc}", file=sys.stderr)
        return 1

    state = load_state(cfg.state_path)
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        log.info("signal %s received, stopping after this tick", signum)

    if not args.once:
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

    with httpx.Client() as client:
        last_refresh = None
        while True:
            now = datetime.datetime.now(datetime.UTC)
            due = last_refresh is None or (now - last_refresh).total_seconds() >= cfg.refresh_seconds
            run_once(cfg, client, state, now, refresh=due)
            if due:
                last_refresh = now
            if args.once or stopping:
                return 0
            time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the loop tests to verify they pass**

Run: `.venv/bin/pytest tests/notifier/test_main.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: every existing test still passes, plus 81 new ones.

- [ ] **Step 6: Commit**

```bash
git add notifier/__main__.py tests/notifier/test_main.py
git commit -m "feat(notifier): run the tick loop

Refreshes per source rather than all-or-nothing, so a Draft outage cannot
suppress an FPL deadline; a source that fails keeps its previous copy.

A failed send is not recorded. The next tick retries and keeps retrying until
it succeeds or the moment passes and retirement takes over -- a Telegram
outage postpones an alert, it never consumes one.

Retirements are recorded even when nothing is sent, so a restart after a long
outage settles in one tick instead of re-evaluating every minute."
```

---

### Task 8: Deployment and documentation

**Files:**
- Create: `notifier/deploy/fpl-notifier.service`
- Create: `notifier/deploy/README.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: `python -m notifier` and `python -m notifier --once` from Task 7.
- Produces: nothing importable.

- [ ] **Step 1: Verify the service actually runs end to end**

Before documenting it, confirm the entrypoint works against the real APIs. This sends a real
Telegram message only if something is genuinely due, which on most days it is not — the point is
that it refreshes, writes state, and exits zero.

```bash
cd "/Users/yevheniistepaniuk/Documents/Personal Projects/Fantasy PL"
NOTIFIER_STATE_PATH=/tmp/notifier-smoke.json \
TELEGRAM_BOT_TOKEN=dummy TELEGRAM_CHAT_ID=dummy \
  .venv/bin/python -m notifier --once
cat /tmp/notifier-smoke.json | head -20
```

Expected: exit 0, a log line per refresh, and a state file listing real moments for both games. A
`ConfigError` here means Task 1 regressed; an empty `cached` means Task 2's live shape check needs
rerunning.

- [ ] **Step 2: Write the systemd unit template**

Create `notifier/deploy/fpl-notifier.service`:

```ini
# Template, not an installed unit. Copy to /etc/systemd/system/, edit the three paths,
# then: systemctl daemon-reload && systemctl enable --now fpl-notifier
[Unit]
Description=FPL and Draft PL deadline notifier
# Only orders startup; the service handles a missing network by holding its cache.
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=CHANGEME
WorkingDirectory=/CHANGEME/path/to/Fantasy PL
EnvironmentFile=/CHANGEME/path/to/Fantasy PL/.env
ExecStart=/CHANGEME/path/to/Fantasy PL/.venv/bin/python -m notifier
Restart=always
RestartSec=30
# Restarting freely is safe: sent keys are on disk, so a restart cannot resend, and
# stale suppression means a long outage produces silence rather than a burst.

# Python buffers stdout when it is a pipe, which would hold log lines back from
# journalctl for minutes at a time on a service this quiet.
Environment=PYTHONUNBUFFERED=1

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/CHANGEME/path/to/Fantasy PL/data

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: Write the deployment notes**

Create `notifier/deploy/README.md`:

```markdown
# Running the notifier

The notifier is a separate process from the Streamlit dashboard. It shares the repo and
the virtualenv, and nothing else — no database, no imports. The dashboard can be down,
mid-deploy, or uninstalled and the alerts keep arriving.

## One-time Telegram setup

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts, and
   copy the token it prints into `TELEGRAM_BOT_TOKEN`.
2. Message [@userinfobot](https://t.me/userinfobot) and copy the `Id` it replies with
   into `TELEGRAM_CHAT_ID`.
3. **Send your bot any message.** Telegram will not let a bot open a conversation, so
   until you do, every send fails with `chat not found`.

Check it:

    .venv/bin/python -m notifier --once

Exit 0 and a `data/notifier_state.json` listing both games means it is working. A
`chat not found` error means step 3 was skipped.

## systemd

Copy `fpl-notifier.service` to `/etc/systemd/system/`, replace every `CHANGEME`, then:

    sudo systemctl daemon-reload
    sudo systemctl enable --now fpl-notifier
    journalctl -u fpl-notifier -f

## macOS launchd

systemd is not available. Run it under a `launchd` agent with `KeepAlive`, or in a
terminal multiplexer if the machine is a desktop that stays awake. Note that a sleeping
Mac sends nothing — the design assumes an always-on host.

## What it sends

Four messages per gameweek, at 24 hours and 2 hours before each of the gameweek deadline
and the Draft waiver deadline. See the spec's timetable:
`docs/superpowers/specs/2026-08-22-deadline-notifier-design.md`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Refuses to start, names a variable | That variable is unset or blank in `.env`. |
| `chat not found` | You have not messaged the bot yet. |
| Nothing arrives, logs are quiet | Normal between alerts. `cat data/notifier_state.json` to see the cached deadlines and the keys already handled. |
| An alert never arrived | Its key is in `sent` — either it was delivered, or it was retired because the process was down until after the moment passed. |
| Alerts stopped after an edit | `.venv/bin/pytest tests/notifier -q`. The isolation test catches an accidental `app/` import. |
```

- [ ] **Step 4: Add a section to the top-level README**

In `README.md`, after the existing `## Run` section and before `## Test`, insert:

```markdown
## Deadline notifier

A separate always-on service that sends a Telegram message 24 hours and 2 hours before
each gameweek deadline and each Draft PL waiver deadline. It shares the repo and the
virtualenv with the dashboard and nothing else — no database, no imports — so one can be
down without touching the other.

    .venv/bin/python -m notifier --once   # check the setup
    .venv/bin/python -m notifier          # run it

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` first; `notifier/deploy/README.md`
covers BotFather, systemd and the failure modes.
```

- [ ] **Step 5: Confirm the suite is still green**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add notifier/deploy/ README.md
git commit -m "docs(notifier): deployment template and setup notes

Restart=always is safe because sent keys are on disk: a restart cannot resend,
and stale suppression turns a long outage into silence rather than a burst.

PYTHONUNBUFFERED because Python buffers stdout to a pipe, which would hold
journalctl output back for minutes on a service this quiet.

The setup notes spell out messaging the bot once. Telegram will not let a bot
open a conversation, and skipping it fails with 'chat not found' -- which
reads like a wrong chat id rather than a missing handshake."
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| Independence (no `app/` imports) | 1 — enforced by `test_isolation.py` |
| Configuration table, loud startup failure | 1 |
| `.gitignore`, `.env.example` | 1 |
| `Moment`, both sources, shape difference | 2 |
| Merging sources on exact equality | 2 |
| `Alert`, offsets, key format | 3 |
| Same-tick merge, stale suppression | 3 (rules) + 4 (rendering) |
| Rendering, timezone, `games` line | 4 |
| Per-source cache in one state file | 5 |
| Corrupt state degrades to empty | 5 |
| Telegram retries and backoff | 6 |
| Tick loop, hourly refresh | 7 |
| Failure table, all seven rows | 2, 5, 6, 7 |
| Running it, systemd template | 8 |
| All 13 listed test cases | 2, 3, 4, 5, 6, 7 |

No gaps.

**Deviations from the spec, both deliberate:**

1. `render.py` is a module of its own rather than part of `schedule.py`. Formatting is pure and separable, and the scheduling rules read better without copy in the middle. The spec's architecture listing has been updated to match.
2. `--once` is a flag on `main`. The spec does not mention it; it is what makes the deploy smoke test in Task 8 and the wiring test in Task 7 possible without a loop that never returns.

**Type consistency:** `Moment(kind, gw, when, games)` and `Alert(moment, offset_hours)` are constructed positionally in Tasks 3–7 exactly as defined in Tasks 2–3. `fetch_source(client, game)`, `merge(*groups)`, `due_alerts(moments, sent, now)`, `format_message(alerts, tz)`, `load_state(path)`, `save_state(path, state)`, `send_message(client, token, chat_id, text, sleep=...)` and `run_once(cfg, client, state, now, refresh)` are each called with the signature their producing task defines. `Config` field names match between `config.py` and every use in `__main__.py` and `test_main.py`.

**Placeholder scan:** none. Every `CHANGEME` in the systemd unit is intentional template content, described as such in the file's first comment and in the deploy notes.
