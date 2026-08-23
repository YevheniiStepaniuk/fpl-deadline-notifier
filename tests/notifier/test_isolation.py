import pathlib

_NOTIFIER = pathlib.Path(__file__).resolve().parents[2] / "notifier"


def test_no_module_in_notifier_imports_from_app():
    """The spec's Independence section: a refactor inside app/ must not be able to stop
    the alerts. The two packages share fetch logic by copy, on purpose. This test is
    what makes that a decision rather than an accident waiting to be undone."""
    offenders = []
    for path in sorted(_NOTIFIER.rglob("*.py")):
        source = path.read_text()
        for lineno, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("import app", "from app")):
                offenders.append(f"{path.name}:{lineno}: {stripped}")
    assert offenders == []
