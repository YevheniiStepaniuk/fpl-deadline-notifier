"""Detecting the bot being added to a chat, via `getUpdates`.

Split from the network the same way `sources.py` splits `parse_fpl` from
`fetch_source`: `parse_added` is pure, so the add/demote/promote cases are testable
without a socket. This service still never *reads* Telegram in the conversational
sense -- it only watches for the one event type that tells it a new chat exists.
"""

import dataclasses
import json

import httpx

from notifier.telegram import api_url

TIMEOUT = 15.0

# Passed as `allowed_updates` to getUpdates so Telegram queues only membership changes.
# Without this, an ordinary channel post or group message would sit in the update
# backlog for a service that has no code path that ever reads it -- growing forever
# and eventually forcing an offset jump just to clear it.
ALLOWED_UPDATES = ["my_chat_member"]

# A `my_chat_member` update fires on every status transition, not just an add: a
# promotion from member to administrator is one of these too, and so is a demotion.
# An "add" is specifically a move from one of these sets to the other. "restricted" is
# deliberately absent from both -- see `_is_present`/`_is_absent` below, since that one
# status can mean either depending on a separate field.
PRESENT = {"member", "administrator", "creator"}
ABSENT = {"left", "kicked"}

# getUpdates statuses that will not clear on their own: a bad or revoked token, or a
# webhook registered on this token (handled separately, since Telegram reports that one
# as a bare 409 rather than a member of this set). Named the same way telegram.py names
# `_PERMANENT`, for the same reason -- retrying these on the next tick wastes a request
# and a log line for an outcome that has already been decided.
_PERMANENT_STATUSES = {401, 404}


@dataclasses.dataclass(frozen=True)
class ChatAdded:
    chat_id: int
    title: str | None
    chat_type: str  # "channel" | "group" | "supergroup" | "private"


class PermanentPollError(Exception):
    """getUpdates failed in a way no later tick will fix on its own.

    The caller (`_greet`) stops polling for the rest of the process's life on one of
    these rather than logging the same warning every tick forever. Fixing the cause --
    a bad token, a lingering webhook -- needs a restart anyway, and a restart is also
    what clears this and lets polling resume.
    """


class WebhookConflictError(PermanentPollError):
    """getUpdates returned 409: a webhook is registered on this token.

    Polling and a webhook are mutually exclusive on the same bot token, and Telegram
    reports the clash as a bare 409 -- naming it here means the cause is in the log
    instead of something someone has to go and search for.
    """


def _describe(response: httpx.Response) -> str:
    # Mirrors telegram.py's own `_describe`, kept as a small duplicate rather than a
    # cross-module import of a private helper: this module's failure messages are its
    # own business, not telegram.py's to define.
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("description", response.text))[:200]
        return response.text[:200]
    except ValueError:
        return response.text[:200]


def _is_present(member: dict) -> bool:
    status = member.get("status")
    if status == "restricted":
        # Telegram overloads "restricted" for both "still in the chat, with limits"
        # and "removed via a restriction" -- `is_member` is the field that actually
        # says which. Treating the bare status string as present (like the other
        # PRESENT entries) would miss a real add whenever the chat's default
        # permissions land a freshly-added bot in "restricted" instead of "member".
        return bool(member.get("is_member"))
    return status in PRESENT


def _is_absent(member: dict) -> bool:
    status = member.get("status")
    if status == "restricted":
        return not member.get("is_member")
    return status in ABSENT


def parse_added(payload: dict) -> tuple[list[ChatAdded], int | None]:
    """Pull add-events out of a getUpdates payload, plus the highest update_id seen.

    Unlike `sources.parse_fpl`, which raises on a missing container precisely because
    an empty result there would be misread as "this game has no deadlines" and
    overwrite a good cache, an empty or malformed result here genuinely means nothing
    happened: there is no cache to corrupt, so skipping the bad entry (or the whole
    payload) is correct rather than a compromise.

    The highest update_id is returned across *every* update in the payload, not only
    the ones that parsed as an add -- a malformed entry still has to be acknowledged
    on the next call, or it would be redelivered forever and block anything after it.
    """
    if not isinstance(payload, dict):
        return [], None
    result = payload.get("result")
    if not isinstance(result, list):
        return [], None

    added: list[ChatAdded] = []
    highest: int | None = None
    for update in result:
        if not isinstance(update, dict):
            continue
        update_id = update.get("update_id")
        if isinstance(update_id, int) and not isinstance(update_id, bool):
            highest = update_id if highest is None else max(highest, update_id)

        member = update.get("my_chat_member")
        if not isinstance(member, dict):
            continue
        chat = member.get("chat")
        new = member.get("new_chat_member")
        old = member.get("old_chat_member")
        if not isinstance(chat, dict) or not isinstance(new, dict) or not isinstance(old, dict):
            continue
        if not _is_present(new) or not _is_absent(old):
            continue
        chat_id = chat.get("id")
        chat_type = chat.get("type")
        if not isinstance(chat_id, int) or not isinstance(chat_type, str):
            continue
        title = chat.get("title")
        if not isinstance(title, str):
            # Gated like every other field pulled off an untrusted payload: a
            # malformed `"title"` must not sail through and land in a message text
            # as `str({'a': 1})`, since format_intro's `str | None` annotation is a
            # promise this is where it gets kept.
            title = None
        added.append(ChatAdded(chat_id=chat_id, title=title, chat_type=chat_type))
    return added, highest


def fetch_added(
    client: httpx.Client, token: str, offset: int | None
) -> tuple[list[ChatAdded], int | None]:
    """Short-poll getUpdates once and return the add-events plus the highest update_id
    seen, exactly as `parse_added` returns it -- raw, not incremented. Converting that
    into the next call's offset (highest + 1) is the caller's job, the same place that
    owns `state.update_offset`.

    `timeout=0` asks Telegram for a short poll rather than its default long poll, so
    this call returns immediately and fits inside the existing tick loop with no
    second thread. Raises `PermanentPollError` (or its `WebhookConflictError`
    subclass) for a failure the caller should stop retrying; anything else -- a
    network blip, a 5xx -- propagates as a plain httpx error for the caller to isolate
    and retry next tick, exactly as `sources.fetch_source` leaves raising to itself.
    """
    params: dict[str, object] = {
        "allowed_updates": json.dumps(ALLOWED_UPDATES),
        "timeout": 0,
    }
    # Telegram expects the *next* offset to be one past the highest update_id it has
    # already sent -- that is what confirms and clears the previous batch. Passing
    # the same highest value again would redeliver it forever; passing highest + 2
    # would silently drop the update in between. Omitted entirely (None) on the very
    # first call, when there is nothing yet to confirm.
    if offset is not None:
        params["offset"] = offset
    response = client.get(api_url(token, "getUpdates"), params=params, timeout=TIMEOUT)
    if response.status_code == 409:
        raise WebhookConflictError(
            "getUpdates returned 409: a webhook is set on this bot token. Remove the "
            "webhook before polling can receive updates."
        )
    if response.status_code in _PERMANENT_STATUSES:
        raise PermanentPollError(
            f"getUpdates returned {response.status_code}: {_describe(response)}. This "
            "will not fix itself without operator action; polling is disabled until "
            "the process restarts."
        )
    response.raise_for_status()
    return parse_added(response.json())
