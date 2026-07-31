"""
Logging setup. One call, at startup, before anything else emits a line.

Named ``log`` rather than ``logging`` on purpose: a module called ``logging.py``
inside a package is importable as ``memo_ai.logging`` and does not shadow the
standard library under Python 3's absolute imports -- but every reader has to
work that out before trusting any ``import logging`` in the package, and one of
them will eventually be wrong.
"""

import logging
import sys
import time


def configure(level: str) -> None:
    """
    Logs go to stderr, which is where the API's ``LOG_CHANNEL=stderr`` puts its
    own -- so ``docker compose logs`` reads the same way for both runtimes.

    No worker id in the format. It was the obvious thing to add for
    ``replicas: 2``, and it is redundant: compose already prefixes every line
    with the container name (``ai-worker-1  | ...``) in both ``up`` and ``logs``.
    The identity that is *not* free is the one on the Postgres side, so it goes
    into ``application_name`` instead -- see memo_ai/db.py.

    Buffering is handled in the image rather than here: ``PYTHONUNBUFFERED=1`` in
    ai/Dockerfile. Without it stdout to a pipe is block-buffered, and the last
    lines before a hard exit are lost exactly when they matter most.
    """
    logging.basicConfig(
        level=logging.getLevelNamesMapping()[level],
        stream=sys.stderr,
        format="%(asctime)s.%(msecs)03dZ %(levelname)-8s %(name)s: %(message)s",
        # UTC, and explicitly so. The default is local time, which in a container
        # is whatever TZ the image happens to carry -- so worker timestamps would
        # not line up with the `timestamptz` values in the rows they are about.
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # basicConfig's datefmt controls the layout, not the timezone: the struct_time
    # it formats still comes from Formatter.converter, which defaults to
    # time.localtime. Setting the format string alone would produce local time
    # wearing a Z.
    logging.Formatter.converter = time.gmtime
