# FPL Deadline Notifier

Telegram reminders before Fantasy Premier League and Draft PL deadlines.

Four messages a gameweek: a day and two hours before the gameweek transfer deadline, and
the same before the Draft PL waiver window closes. Draft's waiver deadline is the one
most easily missed — it shuts a full day before the deadline and nothing on the Draft
site reminds you.

Also answers `/nextdeadline` on demand, and greets any chat it is added to.

## Why it exists

Missing a deadline costs a whole gameweek, and a dashboard only helps someone who
remembers to open it — the week it matters most is the week you are busy. This pushes.

## Setup

Requires Python 3.12+.

    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev]"
    cp .env.example .env    # fill in the bot token and chat id

Create a bot by messaging [@BotFather](https://t.me/BotFather). Get your chat id from
[@userinfobot](https://t.me/userinfobot), or add the bot to a group and read the id from
the logs. **Send your bot a message once first** — Telegram will not let a bot open a
conversation, so until you do, every send fails with `chat not found`.

## Run

    .venv/bin/python -m notifier --once    # one tick, then exit: the setup check
    .venv/bin/python -m notifier           # the service

Or in Docker, which needs no virtualenv:

    docker compose --env-file .env -f notifier/deploy/docker-compose.yml up -d --build

`--env-file` is required: Compose resolves `.env` relative to the compose file's own
directory, not the one you run the command in.

`notifier/deploy/README.md` covers systemd, the failure modes, and how to register the
`/nextdeadline` command so it appears in Telegram's autocomplete.

## Test

    .venv/bin/pytest

## Design

`docs/superpowers/specs/2026-08-22-deadline-notifier-design.md` is the design of record,
including a **Known limitations** section worth reading before debugging anything
surprising. `docs/superpowers/plans/` holds the implementation plan it was built from.

The shape, briefly:

| Module | Responsibility | Purity |
|---|---|---|
| `config.py` | environment, read at call time | pure |
| `sources.py` | both official APIs → `Moment` | network in |
| `schedule.py` | which alerts are owed | **pure** |
| `render.py` | message text | **pure** |
| `banter.py` | the mini-league sting, twenty fixed lines | **pure** |
| `league.py` | both leagues by id: table, transfers, waivers | network in |
| `ai_banter.py` | the sting written from that, via OpenRouter | pure + one call |
| `state.py` | one JSON file: sent keys, cache, greetings | filesystem |
| `telegram.py` | one `sendMessage`, with retries | network out |
| `updates.py` | `getUpdates`, adds and commands | network in |
| `__main__.py` | the tick loop | wiring |

Every rule worth arguing about lives in a pure function that takes `now` as an argument,
which is why the tests need no clock and no mocking to pin the interesting cases.

## Banter

Each alert carries one line of banter. By default it comes from twenty fixed lines in
`banter.py`, aimed at whoever is listed in `NOTIFIER_ROSTER`.

Set `NOTIFIER_FPL_LEAGUE_ID` and/or `NOTIFIER_DRAFT_LEAGUE_ID` plus `OPENROUTER_API_KEY`
and the line is written instead from the league's live state: the table, and every
manager's latest transfers and waiver claims — including the waivers they *lost*, which
is the most mockable thing either API publishes. One manager is picked at random, never
the same one twice running. A roster handle is used to address them when it can be
matched to their manager or team name (whole-string, then per surname), or when
`NOTIFIER_BANTER_ALIASES` maps the handle to the name outright — needed for a handle that
shares no letters with its owner, which is most of them. Unmapped and unmatched, the
line uses their name off the API and nobody gets a notification.

`NOTIFIER_BANTER_MODEL` picks the model for this and this only, so it can differ from
the dashboard's `OPENROUTER_MODEL`. Requests send `reasoning: {enabled: false}` —
ignored by models without a reasoning mode, and on one that has it the difference is
$0.0002 a line against $0.0027, for a joke that gains nothing from deliberation.

The AI path is decoration on top of decoration: no key, no league id, a league API
down, OpenRouter out of credit, a reply in the wrong shape — every one of them falls
back to the twenty lines, and none of them can cost the alert. A line is asked for at
most once every 15 minutes (`ai_banter.COOLDOWN`), because anyone in the chat can type
`/nextdeadline` and it is the operator who pays.

## History

Extracted from a larger Fantasy Premier League project, carrying its own commit history.
It shares no code with that dashboard by design: the notifier has to keep working while
nobody is touching it, and a refactor elsewhere must not be able to stop the alerts.
