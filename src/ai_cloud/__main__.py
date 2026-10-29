# SPDX-License-Identifier: MIT
"""Command line: ``ai-cloud <role>`` or ``python -m ai_cloud <role>``.

Each process role runs from the same package (docs/decisions/0001-architecture.md);
``api`` is the first.
"""

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ai_cloud import software_version
from ai_cloud.entry import api
from ai_cloud.entry.settings import CloudSettings
from ai_cloud.shared.config import CONFIG_DIR_ENV, ConfigError, load_config
from ai_cloud.shared.logs import configure_logging

ENVIRONMENT_ENV = "AI_CLOUD_ENVIRONMENT"

log = logging.getLogger("ai_cloud")


def main(argv: Sequence[str] | None = None) -> int:
    """Run a process role; return the exit status (0: stopped normally, 2: bad
    configuration). uvicorn exits with 3 itself when the api cannot start serving."""
    args = _parse_args(argv)
    try:
        config = load_config(args.environment, args.config_dir)
        settings = CloudSettings.from_config(config)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    configure_logging(settings.logging)
    log.info(
        "starting the %s process",
        args.role,
        extra={"version": software_version(), "environment": args.environment},
    )
    api.run(settings.api)
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ai-cloud", description="Run a process role.")
    parser.add_argument("role", choices=["api"], help="the process role to run")
    parser.add_argument(
        "--environment",
        default=os.environ.get(ENVIRONMENT_ENV),
        help=f"the configuration's environment, such as development (default: ${ENVIRONMENT_ENV})",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        help=f"configuration directory (default: ${CONFIG_DIR_ENV}, or config)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {software_version()}")
    args = parser.parse_args(argv)
    if not args.environment:
        parser.error(f"the environment is required: --environment or ${ENVIRONMENT_ENV}")
    return args


if __name__ == "__main__":
    sys.exit(main())
