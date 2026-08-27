import pathlib

_NOTIFIER = pathlib.Path(__file__).resolve().parents[2] / "notifier"


def test_no_module_in_notifier_imports_from_app():
    """This service began inside a Fantasy Premier League dashboard and was extracted.

    While it lived there, the rule was that a refactor of the dashboard's `app/` package
    must never be able to stop the alerts, so the two shared fetch logic by copy rather
    than by import. The dashboard is gone from this repository, which makes the check
    trivially true today -- and that is exactly why it stays: the two projects are still
    siblings on disk, and the cheapest way to undo the extraction by accident is for
    someone to reach across for the code that is already sitting right there.
    """
    offenders = []
    for path in sorted(_NOTIFIER.rglob("*.py")):
        source = path.read_text()
        for lineno, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("import app", "from app")):
                offenders.append(f"{path.name}:{lineno}: {stripped}")
    assert offenders == []
