"""Command line entry point.

Phase 0 ships one command, `discover`, which is strictly read-only. Later
phases add `scrape`, `sync`, and `serve`. `sync` will never be able to write:
it does not import the apply module.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, load_settings, redact


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def cmd_discover(args: argparse.Namespace) -> int:
    from . import schema_probe

    settings = load_settings()
    result = schema_probe.run_discovery(settings, page_size=args.page_size)
    print(result["text"])
    print(f"Snapshot written to {result['snapshot_path']}")
    print("No POST or PATCH requests were made.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bellhaven-sync", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser(
        "discover",
        help="read-only: inspect the live CRM account schema and save a local snapshot",
    )
    discover.add_argument("--page-size", type=int, default=50)
    discover.set_defaults(func=cmd_discover)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - surface a redacted message, never a raw one
        print(f"Error: {redact(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
