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
    """Raised when a message could not be delivered. State must not be written."""


def api_url(token: str) -> str:
    # Path, not query string: a token in a query string lands in every proxy log on
    # the way.
    return f"https://api.telegram.org/bot{token}/sendMessage"


def _describe(response: httpx.Response) -> str:
    try:
        return str(response.json().get("description", response.text))[:200]
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
        except httpx.HTTPError as exc:
            # The likeliest real failure on a home server is the network, not Telegram.
            last = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code in _PERMANENT:
                raise TelegramError(
                    f"Telegram rejected the message with {response.status_code}: "
                    f"{_describe(response)}"
                )
            if response.status_code == 200:
                body = response.json()
                if body.get("ok"):
                    return
                # Telegram has answered 200 with ok:false. Treating that as delivered
                # would write the sent key and swallow the alert for good.
                last = f"200 but ok=false: {_describe(response)}"
            else:
                last = f"HTTP {response.status_code}: {_describe(response)}"
        if attempt < len(_BACKOFF):
            sleep(_BACKOFF[attempt])
    # The token is deliberately absent from this message: it is logged, and logs get
    # pasted into issues.
    raise TelegramError(f"Telegram send failed after {ATTEMPTS} attempts. Last: {last}")
