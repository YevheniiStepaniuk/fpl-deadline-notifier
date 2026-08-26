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
    """Raised when a message could not be delivered. State must not be written.

    `permanent` distinguishes a status Telegram will not reconsider on a later
    attempt (bot blocked or kicked, chat not found, bad token -- see `_PERMANENT`)
    from simply running out of attempts against a transient failure. The caller uses
    it to decide whether retrying is worth anything at all, rather than retrying a
    chat that will never accept a message again.
    """

    def __init__(self, message: str, *, permanent: bool) -> None:
        super().__init__(message)
        self.permanent = permanent


def api_url(token: str, method: str = "sendMessage") -> str:
    # Path, not query string: a token in a query string lands in every proxy log on
    # the way. Applies equally to getUpdates, which is why `method` is a parameter
    # here rather than a second near-identical function.
    return f"https://api.telegram.org/bot{token}/{method}"


def _describe(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("description", response.text))[:200]
        return response.text[:200]
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
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # The likeliest real failure on a home server is the network, not Telegram.
            # InvalidURL is named separately because it subclasses Exception rather than
            # HTTPError, so a token with a stray space in it would otherwise escape as
            # something the caller does not catch.
            last = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code in _PERMANENT:
                raise TelegramError(
                    f"Telegram rejected the message with {response.status_code}: "
                    f"{_describe(response)}",
                    permanent=True,
                )
            if response.status_code == 200:
                try:
                    body = response.json()
                except ValueError:
                    # A proxy or a captive portal answering 200 with HTML. Symmetric
                    # with _describe: a body we cannot read is a failed send, not an
                    # exception. Anything escaping this function that is not a
                    # TelegramError takes down the tick loop, because that is the only
                    # thing the caller catches.
                    last = f"200 with a body that is not JSON: {response.text[:200]!r}"
                else:
                    # isinstance before .get: valid JSON that is not an object would
                    # raise AttributeError, the same leak by another route.
                    if isinstance(body, dict) and body.get("ok"):
                        return
                    # Telegram has answered 200 with ok:false. Treating that as
                    # delivered would write the sent key and swallow the alert for good.
                    last = f"200 without a success body: {_describe(response)}"
            else:
                last = f"HTTP {response.status_code}: {_describe(response)}"
        if attempt < len(_BACKOFF):
            sleep(_BACKOFF[attempt])
    # The token is deliberately absent from this message: it is logged, and logs get
    # pasted into issues.
    raise TelegramError(
        f"Telegram send failed after {ATTEMPTS} attempts. Last: {last}", permanent=False
    )
