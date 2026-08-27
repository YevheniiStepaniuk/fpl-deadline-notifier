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

## Continuous deployment

Two workflows. `ci.yml` runs the tests on every push and pull request. `deploy.yml`
chains off it with `workflow_run` and only proceeds when CI **succeeded** *and* the
branch was **master** — so a red build or a feature branch goes nowhere. It builds the
image, pushes it to Azure Container Registry, and restarts the container on the VM over
SSH.

Deploys pin the **commit SHA**, not `latest`, so a rollback is a re-run against an older
commit rather than a guess about what `latest` currently means.

### Repository secrets

| Secret | What it is |
|---|---|
| `ACR_LOGIN_SERVER` | e.g. `myregistry.azurecr.io` |
| `ACR_USERNAME` | ACR admin user, or a service principal's app id |
| `ACR_PASSWORD` | its password |
| `VM_HOST` | hostname or IP of the VM |
| `VM_USER` | SSH user |
| `VM_PASSWORD` | that user's password |
| `VM_PORT` | optional, defaults to `22` |
| `VM_KNOWN_HOSTS` | optional but recommended — see below |

Enable the registry admin user with
`az acr update -n <registry> --admin-enabled true`, then read the credentials with
`az acr credential show -n <registry>`.

**`VM_KNOWN_HOSTS` is worth setting.** Without it the workflow falls back to
`ssh-keyscan`, which trusts whatever answers on the first connection — a
machine-in-the-middle at that moment would be accepted, and it would then hold your VM
password. Generate the value on a machine you already trust:

    ssh-keyscan -p 22 your.vm.host

### What is deliberately *not* a GitHub secret

The bot token, chat id and roster. Those live in `/opt/fpl-notifier/.env` on the VM and
never pass through CI. GitHub needs enough to push an image and restart a container; it
has no business holding the token the bot actually speaks with. Create that file from
`.env.example` before the first deploy — the deploy script fails loudly if it is missing
rather than starting a notifier that cannot send anything.

### One-time VM setup

    sudo mkdir -p /opt/fpl-notifier
    sudo cp .env.example /opt/fpl-notifier/.env
    sudo nano /opt/fpl-notifier/.env      # fill in the token and chat id
    sudo chmod 600 /opt/fpl-notifier/.env

The VM needs Docker, and the SSH user needs to be able to run it — either in the
`docker` group or via passwordless sudo.

### A note on password SSH

Key-based authentication is the better choice: a key cannot be brute-forced or shoulder
-surfed, and it can be scoped to this one deploy. This uses a password because that is
what the setup called for, via `sshpass`. If you move to a key later, swap `VM_PASSWORD`
for a `VM_SSH_KEY` secret and drop `sshpass` from the workflow — the rest is unchanged.

### Migrating from a local compose deployment

The compose file creates a volume named `deploy_notifier-state`; the deploy script uses
`fpl-notifier-state`. If you are moving an existing deployment and want to keep its
record of which alerts have already been sent, copy the data across first:

    docker run --rm -v deploy_notifier-state:/from -v fpl-notifier-state:/to \
      alpine sh -c "cp -a /from/. /to/"

Skip it and the first tick treats every past alert as stale and retires it silently —
no burst of late messages, just a fresh start.

## systemd

Copy `fpl-notifier.service` to `/etc/systemd/system/`, replace every `CHANGEME`, then:

    sudo systemctl daemon-reload
    sudo systemctl enable --now fpl-notifier
    journalctl -u fpl-notifier -f

**If your checkout path contains a space** — this project's default one does, twice —
keep the double quotes already present on `ExecStart` and `ReadWritePaths`. systemd
splits both of those on whitespace, so an unquoted path fails to start the service and
silently drops the write permission `data/` needs. `systemd-analyze verify
fpl-notifier.service` catches the first problem before you enable anything; the second
shows up only as a permission error at the first save. Cloning to a path without spaces
avoids the question entirely and is the easier route if you have the choice.

## Docker

The image carries `httpx` and nothing else — that is the notifier's only third-party
import, so the dashboard's streamlit/pandas/anthropic stack stays out of it. 220MB,
running as uid 10001 with a read-only root filesystem and all capabilities dropped.

    docker compose --env-file .env -f notifier/deploy/docker-compose.yml up -d --build
    docker compose --env-file .env -f notifier/deploy/docker-compose.yml logs -f

`--env-file .env` is required, not decoration. Compose resolves `.env` relative to the
compose file's own directory, so without it Compose looks for `notifier/deploy/.env` and
finds nothing. The `${...:?}` guards then abort the run naming the missing variable,
which is the failure you want rather than a notifier that starts and never sends — but
it looks like a missing token when it is really a missing flag.

Check it first, which needs no compose file:

    docker build -f notifier/deploy/Dockerfile -t fpl-notifier .
    docker run --rm -v fpl-notifier-state:/data \
      -e TELEGRAM_BOT_TOKEN=... -e TELEGRAM_CHAT_ID=... fpl-notifier --once

Two things about it worth knowing.

The compose file names the three variables it needs individually instead of using
`env_file: .env`. That file also holds `ANTHROPIC_API_KEY` and `OPENROUTER_API_KEY` for
the dashboard, and there is no reason to put either inside a container that talks only to
Telegram and the two Premier League APIs. Compose still reads `.env` for the `${...}`
values, so nothing changes about where you keep them.

The state volume is a *named* volume, deliberately. The container runs unprivileged, so a
bind mount would arrive owned by your host user and the first save would fail with
`EACCES` — which this service survives by design, logging and carrying on, meaning you
would not notice until a restart re-sent an alert. If you do want a bind mount, `chown`
the directory to uid 10001 first.

## macOS launchd

systemd is not available. Run it under a `launchd` agent with `KeepAlive`, or in a
terminal multiplexer if the machine is a desktop that stays awake. Note that a sleeping
Mac sends nothing — the design assumes an always-on host.

## The /nextdeadline command

Anyone in the chat can send `/nextdeadline` to get the next deadline on demand, with the
real time remaining. Asking does **not** consume the scheduled reminder — that still
arrives at its proper time.

The command works whether or not it is registered with Telegram, but registering it puts
it in the autocomplete and the bot's menu so people can find it. That is a one-off call
against the bot, not part of the service, so it does not happen on deploy:

    docker run --rm --env-file .env --entrypoint python fpl-notifier -c "
    import os, httpx
    t = os.environ['TELEGRAM_BOT_TOKEN']
    httpx.post(f'https://api.telegram.org/bot{t}/setMyCommands',
               json={'commands': [{'command': 'nextdeadline',
                                   'description': 'Show the next FPL or Draft deadline'}]})"

One thing to know about delivery. A bot in a group normally only receives messages that
are commands or mention it — Telegram calls this privacy mode, and it is on by default.
If it has been turned off for this bot via BotFather, the bot receives every message in
the group instead. Either way `/nextdeadline` arrives, and non-command messages are
parsed and discarded without being logged or stored; but it is worth knowing which mode
your bot is in if the group has people in it. Check with `getMe` and read
`can_read_all_group_messages`: true means privacy mode is off.

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
| Under Docker: nothing in the volume | The container could not write `/data`. Named volume, or `chown` the bind mount to uid 10001. |
