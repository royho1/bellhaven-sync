"""Read-only reconciliation pipeline used by ``cli sync``.

Scrapes the public site, reads CRM accounts via the GET-only client, matches,
generates proposals, and persists them locally. Never imports apply capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import crm_client, matching, proposals, scraper
from .config import Settings
from .proposals import ProposalBatch
from .store import ProposalStore, default_db_path

Fetcher = Callable[[str], str]


@dataclass
class SyncResult:
    run_id: int
    batch: ProposalBatch
    match_count: int
    account_count: int
    facility_count: int
    parent_resolved: bool
    db_path: Path


def run_sync(
    settings: Settings,
    *,
    accounts: list[dict[str, Any]] | None = None,
    fetch: Fetcher | None = None,
    base_url: str | None = None,
    db_path: Path | None = None,
    enrich_pages: bool = True,
    session: Any = None,
) -> SyncResult:
    """Execute one read-only reconciliation run and persist proposals."""
    started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if accounts is None:
        account_list = list(crm_client.iter_accounts(settings=settings, session=session))
    else:
        account_list = list(accounts)

    scrape = scraper.scrape_bellhaven(
        base_url=base_url or scraper.DEFAULT_SITE_BASE,
        fetch=fetch,
        data_dir=settings.data_dir,
        enrich_pages=enrich_pages,
    )

    parent = matching.resolve_parent(
        account_list,
        override_id=settings.bellhaven_parent_account_id,
    )
    matches = matching.match_facilities(scrape.facilities, account_list)
    duplicates = matching.find_duplicates(account_list)

    batch = proposals.generate_proposals(
        scrape=scrape,
        accounts=account_list,
        matches=matches,
        parent=parent,
        duplicates=duplicates,
    )
    batch.generated_at = started

    path = db_path or default_db_path(settings.data_dir)
    store = ProposalStore(path)
    run_id = store.save_run(batch, started_at=started)

    matched = sum(1 for m in matches if m.account is not None and not m.ambiguous)
    return SyncResult(
        run_id=run_id,
        batch=batch,
        match_count=matched,
        account_count=len(account_list),
        facility_count=scrape.facility_count,
        parent_resolved=parent.resolved,
        db_path=path,
    )


def format_sync_summary(result: SyncResult) -> str:
    summary = result.batch.summary
    lines = [
        f"Run id:              {result.run_id}",
        f"Database:            {result.db_path}",
        f"CRM accounts:        {result.account_count}",
        f"Website facilities:  {result.facility_count}",
        f"Matched:             {result.match_count}",
        f"Updates proposed:    {summary.get('update_fields', 0)}",
        f"Creates proposed:    {summary.get('create_account', 0)}",
        f"Re-parent proposals: {summary.get('reparent', 0)}",
        f"CHOW proposals:      {summary.get('chow_create_and_link', 0)}",
        f"Ambiguous review:    {summary.get('review_ambiguous', 0)}",
        f"Duplicates:          {summary.get('review_duplicate', 0)}",
        f"Possible stale:      {summary.get('review_stale_or_missing', 0)}",
        f"CHOW human-review:   {summary.get('review_chow_ambiguous', 0)}",
        f"Inactive matches:    {summary.get('review_inactive_account', 0)}",
        f"Parent resolved:     {result.parent_resolved}",
        f"Scrape complete:     {result.batch.scrape_complete}",
        f"Blockers:            {summary.get('blockers', 0)}",
    ]
    for blocker in result.batch.blockers:
        lines.append(f"BLOCKER: {blocker}")
    lines.append("No CRM writes were attempted.")
    return "\n".join(lines)
