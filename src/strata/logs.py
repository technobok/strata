"""Logging for Strata's processes."""

import logging

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure() -> None:
    """Send INFO and above to stderr, which is the container's log.

    strata-worker and strata-web both call it. Without it a gunicorn worker drops
    Strata's INFO lines: gunicorn sets up only its own loggers, and Python's root
    logger passes WARNING and above.
    """
    logging.basicConfig(level=logging.INFO, format=FORMAT)
