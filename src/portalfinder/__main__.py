"""Allow ``python -m portalfinder``, which works even when the script isn't on PATH."""

from portalfinder.cli import app

app(prog_name="portalfinder")
