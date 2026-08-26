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
# An "add" is specifically a move from one of these sets to the other.
PRESENT = {"member", "administrator", "creator"}
ABSENT = {"left", "kicked"}


@dataclasses.dataclass(frozen=True)
class ChatAdded:
    chat_id: int
    title: str | None
    chat_type: str  # "channel" | "group" | "supergroup" | "private"


class WebhookConflictError(Exception):
    """getUpdates returned 409: a webhook is registered on this token.

    Polling and a webhook are mutually exclusive on the same bot token, and Telegram
    reports the clash as a bare 409 -- naming it here means the cause is in the log
    instead of something someone has to go and search for.
    """


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
        if new.get("status") not in PRESENT or old.get("status") not in ABSENT:
            continue
        chat_id = chat.get("id")
        chat_type = chat.get("type")
        if not isinstance(chat_id, int) or not isinstance(chat_type, str):
            continue
        added.append(ChatAdded(chat_id=chat_id, title=chat.get("title"), chat_type=chat_type))
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
    second thread. Raises on any httpx failure; the caller isolates it, exactly as
    `sources.fetch_source` leaves raising to itself and isolation to `_refresh`.
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
    response.raise_for_status()
    return parse_added(response.json())
