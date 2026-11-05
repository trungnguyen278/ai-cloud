# SPDX-License-Identifier: MIT
"""Command line: ``ai-cloud <command>`` or ``python -m ai_cloud <command>``.

Each process role runs from the same package (docs/decisions/0001-architecture.md):
``api`` serves, ``worker`` relays the modules' events and hands them to their consumers,
and ``migrate`` brings the database's tables up to date before a deployment's processes
start. ``signing-key`` makes a key for the cloud's tokens.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ai_cloud import software_version
from ai_cloud.entry import api, migrate, worker
from ai_cloud.entry.settings import CloudSettings
from ai_cloud.shared.config import CONFIG_DIR_ENV, ConfigError, load_config
from ai_cloud.shared.database import DATABASE_URL_ENV, database_url
from ai_cloud.shared.jose import SIGNING_KEYS_ENV, new_signing_key
from ai_cloud.shared.logs import configure_logging
from ai_cloud.shared.secrets import read_secret

ENVIRONMENT_ENV = "AI_CLOUD_ENVIRONMENT"

log = logging.getLogger("ai_cloud")


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command; return the exit status (0: done, or stopped normally; 2: bad
    configuration). uvicorn exits with 3 itself when the api cannot start serving."""
    args = _parse_args(argv)
    if args.command == "signing-key":
        print(json.dumps({"keys": [new_signing_key()]}, indent=2))
        return 0
    try:
        settings = CloudSettings.from_config(load_config(args.environment, args.config_dir))
        if args.command == "api":
            secrets = api.ApiSecrets.read()
        elif args.command == "worker":
            worker_secrets = worker.WorkerSecrets.read()
        else:
            url = read_secret(DATABASE_URL_ENV)
            database_url(url)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    configure_logging(settings.logging)
    log.info(
        "starting the %s process",
        args.command,
        extra={"version": software_version(), "environment": args.environment},
    )
    if args.command == "api":
        api.run(settings, secrets)
    elif args.command == "worker":
        worker.run(settings, worker_secrets)
    elif args.revision is not None:
        [module] = [each for each in migrate.MODULES if each.schema == args.module]
        path = asyncio.run(migrate.revise(url, settings.database, module, args.revision))
        print(path)
    else:
        asyncio.run(migrate.migrate(url, settings.database))
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ai-cloud", description="Run a process role.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {software_version()}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    roles = {
        "api": commands.add_parser("api", help="serve the devices' and the apps' requests"),
        "worker": commands.add_parser(
            "worker", help="relay the modules' events, and hand them to their consumers"
        ),
        "migrate": commands.add_parser(
            "migrate", help="bring every module's tables to its newest migration"
        ),
    }
    for role in roles.values():
        role.add_argument(
            "--environment",
            default=os.environ.get(ENVIRONMENT_ENV),
            help=f"the configuration's environment, such as development (default: "
            f"${ENVIRONMENT_ENV})",
        )
        role.add_argument(
            "--config-dir",
            type=Path,
            help=f"configuration directory (default: ${CONFIG_DIR_ENV}, or config)",
        )
    roles["migrate"].add_argument(
        "--revision",
        metavar="MESSAGE",
        help="write --module's next migration instead, from what its tables declare and "
        "the database lacks",
    )
    roles["migrate"].add_argument(
        "--module", choices=[each.schema for each in migrate.MODULES], help="with --revision"
    )
    commands.add_parser(
        "signing-key",
        help=f"print a new key for the cloud's tokens, as a JWK set ({SIGNING_KEYS_ENV})",
    )
    args = parser.parse_args(argv)
    if args.command in roles:
        if not args.environment:
            roles[args.command].error(
                f"the environment is required: --environment or ${ENVIRONMENT_ENV}"
            )
        if args.command == "migrate" and (args.revision is None) != (args.module is None):
            roles["migrate"].error("--revision and --module go together")
    return args


if __name__ == "__main__":
    sys.exit(main())
