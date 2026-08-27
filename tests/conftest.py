import os

import pytest

_OWNED_PREFIXES = ("TELEGRAM_", "NOTIFIER_")


@pytest.fixture(autouse=True, scope="session")
def hide_the_real_environment():
    """Make a developer's own settings invisible to the whole test session.

    `notifier/config.py` reads `os.environ` when called rather than at import, which is
    most of what makes these tests safe. But a developer with TELEGRAM_BOT_TOKEN
    exported in their shell would still have `load_config()` pick it up, and a test
    asserting on defaults would then pass or fail depending on whose machine it ran on.
    Worse, a test that got as far as a real send would be spending someone's real bot.

    So every variable this project owns is removed for the session. Tests see exactly
    what they set through `monkeypatch` or pass in explicitly, and nothing else.
    """
    with pytest.MonkeyPatch.context() as mp:
        for key in list(os.environ):
            if key.startswith(_OWNED_PREFIXES):
                mp.delenv(key, raising=False)
        yield
