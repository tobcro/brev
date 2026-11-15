"""Entry point."""

from __future__ import annotations

import logging
import os
import sys


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    demo = "--demo" in argv or os.environ.get("BREV_DEMO") == "1"
    argv = [a for a in argv if a != "--demo"]
    logging.basicConfig(level=logging.DEBUG if os.environ.get("BREV_DEBUG") else logging.WARNING)

    from .application import BrevApplication

    app = BrevApplication(demo=demo)
    try:
        app.register(None)
    except Exception:
        pass  # run() reports registration problems itself
    if app.get_is_remote():
        print("Brev is already running; showing its window. Quit it (Ctrl+Q) to start a new version.",
              file=sys.stderr)
    return app.run(argv)
