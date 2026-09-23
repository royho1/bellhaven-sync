"""Explicit CRM write command.

Real writes require both ``DRY_RUN=false`` and ``--execute``.
Either gate alone prints a plan, except ``--execute`` while ``DRY_RUN`` is
still true, which refuses instead of writing.

The scheduled sync command never imports this module.

    python -m bellhaven_sync.apply_cli
    python -m bellhaven_sync.apply_cli --execute
"""

from __future__ import annotations

import argparse
import sys

from .config import ConfigError, build_session, load_settings, redact


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bellhaven-sync.apply",
        description=(
            "Apply approved proposals. Default is a dry-run plan with zero CRM writes. "
            "Real writes require DRY_RUN=false and --execute together."
        ),
    )
    parser.add_argument("--db", default=None, help="SQLite path (default: data/bellhaven_sync.db)")
    parser.add_argument("--run-id", type=int, default=None, help="reconciliation run (default: latest)")
    parser.add_argument("--proposal-id", type=int, default=None, help="apply one proposal from that run")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform CRM writes; refused unless DRY_RUN=false",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    from .apply import ExecuteWhileDryRunError, format_apply_report, run_apply
    from .store import ProposalStore, default_db_path

    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    store = ProposalStore(args.db or default_db_path(settings.data_dir))
    session = None
    if args.execute and not settings.dry_run:
        session = build_session(settings)
    try:
        report = run_apply(
            settings=settings,
            store=store,
            session=session,
            run_id=args.run_id,
            proposal_id=args.proposal_id,
            execute=args.execute,
        )
    except ExecuteWhileDryRunError as exc:
        print(f"DRY RUN refusal: {exc}", file=sys.stderr)
        print("No CRM writes were attempted.", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - surface a redacted message, never a raw one
        print(f"Error: {redact(exc)}", file=sys.stderr)
        return 1
    print(format_apply_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
