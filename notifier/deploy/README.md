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
