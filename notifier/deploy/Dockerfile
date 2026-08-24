# Build from the repository root, not from this directory:
#   docker build -f notifier/deploy/Dockerfile -t fpl-notifier .
FROM python:3.13-slim

# httpx is the notifier's only third-party import; everything else it uses is in the
# standard library. Installing the project's pyproject.toml instead would drag in
# streamlit, pandas and anthropic for the dashboard -- hundreds of megabytes this
# service never touches, and a much larger surface to keep patched.
RUN pip install --no-cache-dir "httpx>=0.27"

# Measured on python:3.13-slim: 486 zoneinfo keys are present and Europe/London
# resolves, so the tzdata package is not needed. Do not add it "just in case".
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NOTIFIER_STATE_PATH=/data/notifier_state.json

# PYTHONUNBUFFERED because Python buffers stdout when it is a pipe, which it always is
# under `docker logs`. This service is quiet enough between alerts that the delay would
# be minutes, and a quiet service you cannot see is hard to distinguish from a dead one.

WORKDIR /app
COPY notifier/ ./notifier/

# The state file is the only thing written, and it has to outlive the container: it
# records which alerts have already been sent, so losing it re-sends whatever is still
# pending. Owned by the unprivileged user before VOLUME so a named volume inherits it.
RUN useradd --create-home --uid 10001 notifier \
    && mkdir -p /data \
    && chown notifier:notifier /data
VOLUME ["/data"]
USER notifier

# No CMD: the entrypoint takes the module's own flags, so `docker run fpl-notifier
# --once` runs a single tick and exits, which is the setup check.
ENTRYPOINT ["python", "-m", "notifier"]
