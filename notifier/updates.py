"""Detecting the bot being added to a chat, and any `/command` sent to it, via
`getUpdates`.

Split from the network the same way `sources.py` splits `parse_fpl` from
`fetch_source`: `parse_added`/`parse_commands` are pure, so the add/demote/promote
cases and the command-matching rules are testable without a socket. This service
still never *reads* Telegram in the conversational sense -- `allowed_updates` below
means it is handed only membership changes and messages that are themselves commands
or mention the bot, never general chatter.
"""

import dataclasses
import json
import re

import httpx

from notifier.telegram import api_url

TIMEOUT = 15.0

# Passed as `allowed_updates` to getUpdates so Telegram queues only what this service
# ever looks at: membership changes, and commands (from both "message" and
# "channel_post"). Without this, an ordinary channel post would sit in the update
# backlog for a service that has no code path that ever reads it -- growing forever
# and eventually forcing an offset jump just to clear it. Adding "message" does not
# open the floodgates to normal group chatter: bot privacy mode is on by default (and
# this bot has never disabled it), so in a group Telegram hands the bot only messages
# that are commands or that @-mention it -- everyone else's conversation is invisible
# to it regardless of allowed_updates.
#
# "message" and "channel_post" are both required, not a redundant pair: Telegram puts
# a command sent in a group or DM under "message" but a command posted in a channel
# under "channel_post" -- same `Message` shape (`chat`, `text`, etc. are identical
# fields per the Bot API docs), just a different top-level key on the Update object.
# Dropping either one would silently stop /nextdeadline from working in that kind of
# chat while every other chat kept working, which is an easy thing for a future edit
# to do by mistake if this looked like it needed only one.
ALLOWED_UPDATES = ["my_chat_member", "message", "channel_post"]

# `/command` or `/command@some_bot`, matched at the very start of the (whitespace-
# trimmed) text and only there -- "please run /nextdeadline" must not trigger, since
# that is a sentence about the command, not an invocation of it. The optional
# `@botname` suffix is how a command looks in a group when it needs disambiguating
# from another bot's command of the same name; accepted without checking it actually
# names *this* bot, because privacy mode hands every bot in the chat every message
# that starts with "/" regardless of which bot the suffix names, so a stricter check
# here would not stop `/nextdeadline@some_other_bot` from arriving -- it would only
# make this bot ignore its own command when addressed precisely. Whether the *name*
# is one this service recognises is decided by the caller (`__main__`), not here.
_COMMAND_RE = re.compile(r"^/(?P<name>[a-z0-9_]+)(?:@[\w-]+)?(?:\s|$)", re.IGNORECASE)

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


@dataclasses.dataclass(frozen=True)
class Command:
    """One recognised `/command` invocation, from whichever chat sent it.

    A separate type from `ChatAdded`, not a shared "update" grab-bag, so `__main__`
    can decide what to do with each by its own type rather than by inspecting some
    tag field -- the same reason `parse_added` returns a list of `ChatAdded` rather
    than raw dicts. `name` is already lowercased and has any `@botname` suffix
    stripped -- see `_COMMAND_RE` -- so a caller compares it against a plain string
    like `"nextdeadline"` and never has to repeat that normalisation itself.
    """

    chat_id: int
    name: str


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


def parse_commands(payload: dict) -> tuple[list[Command], int | None]:
    """Pull `/command` invocations out of a getUpdates payload, plus the highest
    update_id seen -- the same contract as `parse_added`, deliberately: an empty or
    malformed payload means nothing happened and is skipped rather than raised on, and
    the highest update_id is taken across *every* entry in `result`, not only the ones
    that turned out to be commands, so a malformed or irrelevant update still gets
    acknowledged. Given the same payload, this returns the same `highest` as
    `parse_added` would, since both walk the same `result` list the same way -- the
    caller (`fetch_updates`) relies on that rather than reconciling two numbers.
    """
    if not isinstance(payload, dict):
        return [], None
    result = payload.get("result")
    if not isinstance(result, list):
        return [], None

    commands: list[Command] = []
    highest: int | None = None
    for update in result:
        if not isinstance(update, dict):
            continue
        update_id = update.get("update_id")
        if isinstance(update_id, int) and not isinstance(update_id, bool):
            highest = update_id if highest is None else max(highest, update_id)

        # A command can arrive under either key -- see ALLOWED_UPDATES' own comment
        # on why both are requested. Both are `Message` objects with an identical
        # shape (per the Bot API docs), so reading whichever one is actually present
        # is correct rather than a shortcut: there is nothing channel_post-specific
        # left to handle once this line picks it up.
        message = update.get("message")
        if not isinstance(message, dict):
            message = update.get("channel_post")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(chat, dict) or not isinstance(text, str):
            continue
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            continue
        match = _COMMAND_RE.match(text.strip())
        if match is None:
            continue
        commands.append(Command(chat_id=chat_id, name=match.group("name").lower()))
    return commands, highest


def fetch_updates(
    client: httpx.Client, token: str, offset: int | None
) -> tuple[list[ChatAdded], list[Command], int | None]:
    """Short-poll getUpdates once and return both the add-events and the command
    invocations in this batch, plus the highest update_id seen -- raw, not
    incremented; converting that into the next call's offset (highest + 1) is the
    caller's job, the same place that owns `state.update_offset`.

    One GET, not two: both `parse_added` and `parse_commands` run over the single
    payload this call fetches, rather than each doing their own poll. A second poll
    would consume its own offset and risk skipping over updates the first poll's
    parse never looked at -- the same kind of "advance the offset without acting on
    what it skipped" bug `state.pending_greetings` exists to prevent on the greeting
    side.

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
    payload = response.json()
    added, highest_from_added = parse_added(payload)
    commands, highest_from_commands = parse_commands(payload)
    # Either one alone already covers the whole `result` list (see both docstrings);
    # `is not None` rather than `or` just in case a future edit narrows one parser to
    # only its own update type, where `0 or x` would wrongly fall through to `x`.
    highest = highest_from_added if highest_from_added is not None else highest_from_commands
    return added, commands, highest
