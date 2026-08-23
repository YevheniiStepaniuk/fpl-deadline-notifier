# Deadline notifier — design

**Date:** 2026-08-22
**Status:** approved, ready for an implementation plan

## Why

Missing a deadline costs a whole gameweek. The dashboard only helps someone who remembers to open
it, and the week it matters most is the week you are busy. Nothing in the app pushes; every page
waits to be visited.

The two games have separate clocks and one of them has a second, earlier clock. Classic FPL has a
gameweek deadline. Draft has the same gameweek deadline plus a waiver window that shuts a full day
before it — the one most easily forgotten, because nothing on the Draft site reminds you either.

This adds a small always-on process that sends a Telegram message a day before and two hours
before each of those moments.

## Scope

**In:** a standalone `notifier/` service that polls both official APIs, works out which deadlines
are approaching, and pushes merged Telegram alerts at 24h and 2h.

**Out:**

- Interactive bot commands. No `getUpdates` loop, no `/next`, no `/status`. Push only.
- Multiple users. One chat id from the environment. No subscriber table, no `/subscribe`.
- Draft `trades_time`. The Draft payload carries it and the design accommodates it, but it was not
  asked for. Adding it later is one entry in a list of kinds.
- Any content beyond the deadline itself. No squad summary, no transfer advice, no AI prose. The
  alert says what is due and when.
- Reading or writing `data/fpl.sqlite3`. The notifier shares no state with the dashboard.

## Independence

The notifier is a sibling of `app/`, not a part of it. It imports nothing from `app/` — not
`config`, not `fpl_client`, despite the overlap in what they fetch.

That duplication is deliberate. The dashboard's `fpl_client` exists to feed a sync into SQLite and
will change shape as the dashboard does; the notifier needs two fields out of two endpoints and
must keep working while nobody is touching it. Coupling them would mean a refactor for a Streamlit
page could silently stop the alerts, and the failure would be invisible until a deadline was
missed. Roughly thirty lines of overlap is a fair price for a service that cannot be broken from
elsewhere.

The two processes run independently. The dashboard can be down, mid-deploy, or uninstalled and the
notifier keeps sending.

## Architecture

```
notifier/
  __init__.py
  __main__.py      entrypoint: the tick loop, signal handling, wiring
  config.py        environment: token, chat id, timezone, intervals, paths
  sources.py       both APIs -> list[Moment]
  schedule.py      (moments, sent_keys, now) -> list[Alert]      PURE
  state.py         one JSON file: sent keys + the last-good moments cache
  telegram.py      sendMessage over httpx, with retries
```

| Piece | Purity | Tested with |
|---|---|---|
| `schedule.py` | pure | plain values, an injected `now` |
| `sources.py` | network in, values out | `respx` against captured payloads |
| `telegram.py` | network out | `respx` |
| `state.py` | filesystem | `tmp_path` |
| `__main__.py` | wiring | one smoke test, one tick |

`schedule.py` holds every rule worth arguing about — which offsets fire, what merges, what is too
stale to send. It takes `now` as an argument and returns a list. No clock, no network, no disk. All
the interesting tests live there and none of them need mocking.

## Data

### `Moment`

```python
@dataclass(frozen=True)
class Moment:
    kind: str          # "deadline" | "waivers"
    gw: int
    when: datetime     # timezone-aware, UTC
    games: frozenset   # {"fpl"}, {"draft"}, or {"fpl", "draft"}
```

### Sources

| Game | Endpoint | Fields |
|---|---|---|
| Classic | `https://fantasy.premierleague.com/api/bootstrap-static/` | `events[].id`, `events[].deadline_time` |
| Draft | `https://draft.premierleague.com/api/bootstrap-static` | `events.data[].id`, `.deadline_time`, `.waivers_time` |

Both were called live on 2026-08-22 and both answered unauthenticated. Neither needs a team id, a
league id, or a cookie — deadlines are properties of the game, not of an entry. That is what keeps
the waiver alert cheap: no login to maintain.

Note the shape difference. Classic returns `events` as a list; Draft returns it as a dict with
`current`, `next`, and `data`, where `data` is the list. Each source function normalises to
`Moment` so nothing downstream knows the difference.

### Merging sources

Two moments with the same `kind` and the same UTC instant become one, with `games` unioned.

Live check on 2026-08-22: GW2 is `2026-08-28T17:30:00Z` in both games, and so are GW1 and GW3. In
practice, every gameweek deadline produces one merged moment rather than two. The code does not
assume that — the games have diverged before and a merge on exact equality handles either case
without a special branch.

Draft waivers produce their own moment, since `waivers_time` is a different instant and belongs to
Draft alone.

The merged list is written to the state file after every successful refresh, and read back at
startup. One file holds both the sent keys and this cache, because they are written on the same
schedule and a half-restored pair would be worse than either alone.

## Alerts

Two offsets per moment: **24 hours** and **2 hours**.

```python
@dataclass(frozen=True)
class Alert:
    moment: Moment
    offset_hours: int
```

The key written to state is `{kind}:{gw}:{offset_hours}` — for example `waivers:2:24`. A key is
written only after a successful send, and a key already present is never sent again.

### Rule: alerts due in the same tick merge into one message

Draft's `waivers_before_deadline_hours` is `24`, confirmed in the live `settings.transactions`
payload, and the GW2 numbers bear it out: waivers `2026-08-27T17:30:00Z`, deadline
`2026-08-28T17:30:00Z`.

So the 24-hour warning for a gameweek deadline fires at the exact instant the waiver window shuts.
Sent as two messages, that is a pair of notifications a second apart saying overlapping things.
Every alert due in one tick is therefore rendered as a single message with one line per alert.

### Rule: a trigger whose moment has passed is suppressed

If the process is down when a trigger time passes, the alert is sent on restart **only if the
moment itself is still in the future**. A 2-hour warning for a deadline that passed yesterday is
noise, and a restart after a long outage would otherwise deliver a burst of them at once.

Suppressed alerts are still written to state, so a second restart does not re-evaluate them.

### Rendering

Times render in `Europe/London` by default, overridable with `NOTIFIER_TZ`. London is the right
default because it is the timezone the official sites quote deadlines in, so the message matches
what you see when you click through.

Single alert:

```
⏰ GW2 deadline in 2 hours
FPL + Draft · Fri 28 Aug, 18:30 BST
```

Two alerts in one tick:

```
⏰ Two deadlines

GW2 waiver window closes now
Draft · Thu 27 Aug, 18:30 BST

GW2 deadline in 24 hours
FPL + Draft · Fri 28 Aug, 18:30 BST
```

The `games` line names the games the moment belongs to, so a divergence between FPL and Draft is
visible in the message rather than hidden by the merge.

## The tick loop

```
every POLL_SECONDS (default 60):
    if deadlines older than REFRESH_SECONDS (default 3600):
        refetch both sources; on success, write the disk cache
    due = schedule.due_alerts(moments, state.sent_keys(), now())
    if due:
        send one merged message
        on success: state.mark(due)
```

A minute of granularity against a two-hour warning is fine, and the loop body is a no-op when
nothing is due. Refetching hourly rather than per tick keeps the load on the official APIs at 48
requests a day while still noticing a moved deadline well before any alert for it.

## Failure handling

| Failure | Behaviour |
|---|---|
| Either API fetch fails | Keep the last-good moments from the disk cache, log, retry on the next refresh. A dead API does not stop pending alerts from firing. |
| No disk cache and the first fetch fails | Log and retry. Nothing to send yet; no alert is lost, because none was computable. |
| One API succeeds, the other fails | Use the fresh half and the cached half. A Draft outage must not suppress an FPL deadline. |
| Telegram send fails | Three attempts, exponential backoff (1s, 4s, 16s). State is **not** written, so the next tick retries. |
| Telegram fails all retries | Log. The alert is retried every tick until it succeeds or its moment passes and stale suppression retires it. |
| Deadline moved after its alert was sent | Not resent — the key is per gameweek, not per timestamp. Logged when a cached instant changes. Accepted: the alternative resends on every cosmetic reschedule. |
| State file missing or corrupt | Treat as empty. Stale suppression then retires everything already past, so a lost state file costs at most the alerts still genuinely pending. |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Required. From BotFather. |
| `TELEGRAM_CHAT_ID` | — | Required. Your own chat with the bot. |
| `NOTIFIER_TZ` | `Europe/London` | Timezone the messages render in. |
| `NOTIFIER_POLL_SECONDS` | `60` | Tick interval. |
| `NOTIFIER_REFRESH_SECONDS` | `3600` | How often the APIs are refetched. |
| `NOTIFIER_STATE_PATH` | `data/notifier_state.json` | Sent keys and the moments cache. |

Startup fails loudly with a named variable if the token or chat id is missing. A notifier that
silently does nothing is worse than one that refuses to start.

Both new variables go in `.env.example` with the BotFather steps written out. `.gitignore` already
covers `.env` and every variant. It does **not** cover the new state file — `data/` is only
ignored for `*.sqlite3` and `*.db` — so `data/notifier_state.json` is added to it. The file holds
nothing secret, but it is machine-local and would conflict on every pull.

## Running it

Foreground, for development:

    .venv/bin/python -m notifier

For the always-on box, a unit file is committed as a template rather than installed:
`notifier/deploy/fpl-notifier.service` for systemd, with `Restart=always`. Restarting freely is
safe precisely because state is on disk and stale suppression exists.

## Testing

`pytest` and `respx`, both already dev dependencies. Payload fixtures are trimmed captures of the
real responses taken on 2026-08-22, kept under `tests/notifier/fixtures/`.

Cases that must be covered:

- Classic and Draft deadlines at the same instant merge to one moment with both games.
- Classic and Draft deadlines at different instants stay two moments.
- Draft's `events.data` shape parses; classic's `events` list shape parses.
- A waiver deadline and a gameweek 24-hour warning falling in one tick render as one message.
- An alert already in state is not resent.
- A trigger whose moment has passed is suppressed and marked.
- A trigger whose moment is still ahead is sent late after a restart.
- A failed Telegram send leaves state unwritten; the next tick retries.
- Telegram retries back off and give up after three attempts.
- One source failing falls back to cache for that source only.
- A corrupt state file is treated as empty without raising.
- A deadline in the BST/GMT changeover week renders with the right offset and abbreviation.
