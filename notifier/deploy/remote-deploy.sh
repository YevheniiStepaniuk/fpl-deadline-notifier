#!/usr/bin/env bash
#
# Runs ON THE VM, piped to `bash -s` over SSH by .github/workflows/deploy.yml.
# It expects ACR_LOGIN_SERVER, ACR_USERNAME, ACR_PASSWORD and IMAGE to have been
# exported by the lines the workflow prepends to this file.
#
# Deliberately not idempotent-by-tag: IMAGE is always a specific commit SHA, so
# re-running this pins the same image rather than whatever `latest` has become.
set -euo pipefail

: "${ACR_LOGIN_SERVER:?}" "${ACR_USERNAME:?}" "${ACR_PASSWORD:?}" "${IMAGE:?}"

CONTAINER=fpl-notifier
VOLUME=fpl-notifier-state
ENV_FILE=/opt/fpl-notifier/.env

# The runtime secrets -- bot token, chat id, roster -- live in this file on the VM and
# never pass through CI. GitHub needs registry and SSH credentials to deploy; it has no
# business holding the token the bot actually speaks with.
if [ ! -r "${ENV_FILE}" ]; then
  echo "missing ${ENV_FILE} -- create it from .env.example before the first deploy" >&2
  exit 1
fi

# --password-stdin so the registry password is not in this process's argv.
printf '%s' "${ACR_PASSWORD}" | docker login "${ACR_LOGIN_SERVER}" -u "${ACR_USERNAME}" --password-stdin
trap 'docker logout "${ACR_LOGIN_SERVER}" >/dev/null 2>&1 || true' EXIT

docker pull "${IMAGE}"

# Created explicitly rather than implicitly, so the intent is visible: this volume holds
# which alerts have already been sent. Losing it means re-sending whatever is still
# pending, which is why the container is replaced around it rather than with it.
docker volume create "${VOLUME}" >/dev/null

docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true

docker run -d \
  --name "${CONTAINER}" \
  --restart unless-stopped \
  --env-file "${ENV_FILE}" \
  -e NOTIFIER_STATE_PATH=/data/notifier_state.json \
  -v "${VOLUME}":/data \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
  "${IMAGE}"

# Give it a moment and confirm it is still up. `docker run -d` succeeds the instant the
# container starts, so without this a container that dies immediately -- a bad env file,
# say -- would report as a green deploy.
sleep 5
if [ "$(docker inspect -f '{{.State.Running}}' "${CONTAINER}")" != "true" ]; then
  echo "container is not running after deploy; last lines:" >&2
  docker logs --tail 30 "${CONTAINER}" >&2 || true
  exit 1
fi

echo "deployed ${IMAGE}"
docker logs --tail 5 "${CONTAINER}"

# Reclaim disk from superseded SHA tags. The VM is small and every deploy leaves one.
docker image prune -af --filter "until=168h" >/dev/null 2>&1 || true
