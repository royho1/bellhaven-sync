# PROJECT_STATE

Durable notes for anyone (human or agent) picking up bellhaven-sync. Append
learnings here rather than relying on chat history.

## What this is

A local, human-in-the-loop CRM reconciliation tool for the Clipboard Health analyst
assessment. Scrape Bellhaven Senior Living's public site, read the CRM, propose
corrections, apply only what a human approves.

## Hard rules

- The CHOW rule follows the assessment exactly. When an existing facility must move
  to a different parent and the account has revenue history AND `outstanding_ar > 0`:
  create a NEW account under the correct parent, then PATCH the OLD account with
  `chow_current_account` and nothing else. The old account's `parent_id`, `status`,
  `note`, `name`, and address are never touched. If there is no revenue history or
  `outstanding_ar` is zero, re-parent the existing account directly.
- Do not invent business rules. The revenue and outstanding-AR SOP applies only to
  ownership/parent changes. Duplicates, stale accounts, and inactive accounts get
  evidence surfaced to a human, not an invented AR policy.
- Never pick the Bellhaven parent by child count. Prefer an exact normalized name
  match with no `parent_id`. If more than one plausible parent exists, stop and ask.
- Matching weights address and location evidence over name similarity. A name-only
  agreement never produces a match. State mismatches are a hard gate, which is what
  keeps Carlisle, PA away from New Carlisle, OH.
- The token is never hardcoded, committed, printed, logged, or written into fixtures,
  snapshots, or docs.

## API facts (verified)

- Base URL: `https://analyst-assessment-production.up.railway.app/api/v1`
- Auth: `Authorization: Bearer <CLIPBOARD_API_TOKEN>`
- Endpoints: `GET /me`, `GET /accounts`, `POST /accounts`, `GET /accounts/{id}`,
  `PATCH /accounts/{id}`. There is no DELETE and no merge endpoint.
- `GET /accounts` pagination: `page` (default 1) and `page_size` (default 50).
- `GET /accounts` filters: `q`, `city`, `state`, `zip`, `street`, `parent_id`.
- The OpenAPI spec does NOT document the account schema; request and response bodies
  are effectively empty objects. Field names and types were observed empirically and
  are recorded in `docs/schema-findings.md`.
- Confirmed by the Phase 0 run (121 accounts): the list envelope is
  `{"data": [...], "page", "page_size", "total"}`. The identifier is `account_id`,
  not `id`. Address fields are `billing_street`, `billing_city`, `billing_state`,
  `billing_zip`. Status is `Active` or `Inactive`.
- The API NEVER returns null. Unset fields come back as `""`. Use
  `fields.is_set()`; a blank-unaware check reports every field as populated.
- `lifetime_revenue` and `outstanding_ar` are plain ints, never null, never
  negative. Every account with positive AR also has positive revenue, so the
  ambiguous zero-revenue-with-AR branch does not occur in this data.
- The Bellhaven parent is `0015QAPLGS3FVYEEEM`, named "Bellhaven Senior Living
  (Parent Account)". Name normalization must strip the `(Parent Account)` suffix.
  It is the only match, so the resolver does not have to stop on this data.
- `created_by_candidate` is False on every existing account, which gives the apply
  path a cheap way to recognize accounts this tool created.
- No documented rate limit. Requests are sequential with explicit timeouts; no
  invented throttle.

## Website findings (prior investigation)

- The public site is server-rendered, so requests + BeautifulSoup is sufficient.
- `/communities` lists 34 communities; the homepage claims 35.
- Bellhaven Meadows of Findlay exists but is absent from the normal listing. The
  scraper unions the listing, `sitemap.xml`, and internal links, and asserts against
  the homepage count. If the union comes up short, the run raises a completeness
  blocker and withholds every "missing from website" proposal, so Findlay is never
  mistaken for stale CRM data.
- Individual facility pages carry address and care-offering detail.
- The About page references Harborview Care Group and Cedar Trail acquisitions.
- As of Branch 2 development, `bellhavenseniorliving.com` does not resolve in DNS
  (NXDOMAIN). Scraper behavior is covered by fixture HTML under `tests/fixtures/html/`.
  Live `cli scrape` will fail until the assessment site is reachable; matching and
  normalization do not depend on the live site.

## Architecture decisions

- Python, requests, BeautifulSoup, Flask, SQLite, pytest, python-dotenv. No React, no
  Docker, no async, no LLM calls in the pipeline.
- The write boundary is structural: `crm_client.py` is GET-only forever, all write
  capability lives in `apply.py`, and `tests/test_write_boundary.py` walks the import
  graph to prove the sync path cannot reach it.
- SQLite at `data/bellhaven_sync.db` is the persistence layer, which is why the
  schedule is local cron rather than an ephemeral CI runner.
- Field names funnel through `bellhaven_sync/fields.py` so a schema surprise is a
  one-line change.

## Branch workflow

Four branches, each reviewed and merged before the next starts:
`feat/schema-discovery`, `feat/scrape-and-match`, `feat/proposals-and-review`,
`feat/apply-and-schedule`.

## Current status (2026-09-22)

- Branch 1 `feat/schema-discovery` is merged to `main`.
- Branch 2 `feat/scrape-and-match` is merged to `main` via PR #2 (`4cf89e7`).
- Branch 3 `feat/proposals-and-review` is merged to `main` via PR #3 (`4c90c07`).
- Branch 4 `feat/apply-and-schedule` is the final write path. Do not merge it
  until the Codex review on this branch is clean. Do not run live `--execute`
  against the assessment API.
- Branch 3 decisions:
  1. Explicit proposal action types in `proposals.py` (update/reparent/CHOW/create/
     ambiguous/duplicate/stale/chow-review/inactive-review/care-type-review).
     No invented merge/delete/deactivate.
  2. `chow.py` isolates parent-change policy; zero revenue + positive AR is human
     review; revenue+AR uses two-step CHOW plan; otherwise direct re-parent.
  3. Two-step CHOW suppresses `update_fields` on the old account; website values
     live only in `new_account_template`, and the old account patch is solely
     `chow_current_account`.
  4. Unresolved CHOW (`KIND_HUMAN_REVIEW`: wrong parent, `lifetime_revenue == 0`,
     `outstanding_ar > 0`) also suppresses `update_fields` on that old account.
     The pipeline still emits only `review_chow_ambiguous`. No auto-reparent and
     no auto-CHOW. Direct re-parent may still emit normal field updates.
  5. Ambiguous facilities store the complete candidate account IDs on
     `MatchResult.candidate_account_ids`, taken from the full candidate list
     before display truncation. `runners_up` stays a short evidence list.
     Stale suppression uses the complete ID set only. Those accounts are not
     confident matches and get no update, reparent, or CHOW proposals.
  6. Review status POSTs require a per-session CSRF token (`secrets`, Flask
     `secret_key` generated at app creation, `secrets.compare_digest`). Missing
     or wrong tokens return 403 and do not change SQLite.
  7. `cli serve` / `run_server` bind loopback only (`127.0.0.1`, `localhost`,
     `::1`). `0.0.0.0`, LAN IPs, and other hosts fail before the server starts.
  8. SQLite `data/bellhaven_sync.db` stores runs + proposals; new runs insert rows
     and never overwrite prior review status. Review UI defaults to the latest run.
  9. `cli sync` and `cli serve` are CRM-read-only; approve/reject only updates SQLite.
  10. Incomplete scrape or unresolved parent suppresses create/stale/parent-move
     proposals that depend on that certainty. Unresolved parent also suppresses
     `update_fields` on matched accounts: no reparent, no CHOW, and no
     independently approvable field write until the Bellhaven parent is known.
     A resolved correct parent still emits normal `update_fields`.
  11. A confident match to a CRM account with `status == "Inactive"` emits
     `review_inactive_account` only. `STATUS` stays out of `COMPARABLE_FIELDS`.
     The proposal records the inactive status and match evidence and does not
     set status to Active. Ordinary field diffs may still be proposed when
     existing safety rules allow them. No automatic reactivation.
  12. `normalize_street()` still drops suite/unit identifiers for matching.
     Proposal field comparison uses `normalize_street_for_comparison()`, which
     canonicalizes unit designators (`Suite`/`Ste.`/`Suite #`, `Apartment`/`Apt.`,
     `#`/`Unit`/`Unit #`) and keeps the unit identifier, so `Suite 200` and
     `Suite 100` differ. `Suite #200` is the same unit as `Suite 200`.
  13. Writable `care_type` values stay inside the observed CRM set: Skilled
     Nursing, Assisted Living, Memory Care, Independent Living. Multiple website
     offerings, or an unrecognized offering, become `review_care_type` only.
     They are never joined into a composite, and create/CHOW templates omit
     `care_type` until there is exactly one supported value. No offering is
     chosen automatically.
  14. `normalize_name()` stays lossy for matching. Proposal name comparison uses
     `normalize_name_for_comparison()`, which keeps qualifiers such as Memory
     Care, Assisted Living, and parenthetical words.
     15. Apply lives only in `apply.py`, reached through `python -m bellhaven_sync.apply_cli`.
     `crm_client.py` stays GET-only. `cli sync` and `scripts/run_scheduled_sync.sh`
     cannot import the write path. Writes require an approved writable proposal,
     a fresh CRM preflight, `DRY_RUN=false`, and `--execute`. Review-only actions
     never write. CHOW POSTs a new account, stores that id, then PATCHes the old
     account with only `chow_current_account`. Application attempts are a separate
     SQLite table and do not overwrite approval status.
  16. Uncertain write outcomes (`ApplyError(uncertain=True)`) persist as attempt
     state `uncertain`, not ordinary `failed`/`blocked`. That durable flag is how
     resume knows a prior POST may have succeeded without an id.
  17. Ordinary create and CHOW share the same pre-create scan. Neither issues a
     second POST after an uncertain POST unless a unique tool-created account is
     first recovered. With no persisted `created_account_id`: scan for exact
     matches under the expected parent using approved identity/address evidence.
     Matching non-tool accounts (`created_by_candidate` is not True) are CRM drift
     and block both ordinary create and CHOW create (no adopt, no POST, no PATCH).
     With no non-tool matches: exactly one tool-created match is recovered; more
     than one stops for human review; zero matches with a prior uncertain attempt
     refuse another POST fail-closed; zero matches with no prior uncertain attempt
     may POST once.
  18. Recovery-scan CRM read failures (`crm_client.CrmError` from
     `_matching_accounts_for_create`) become `ApplyError` for the current proposal
     only. The attempt ends blocked; later approved proposals in the same
     `run_apply()` invocation are still considered. No POST/PATCH on an untrusted
     scan.
  19. Execute mode atomically claims a proposal in SQLite (`BEGIN IMMEDIATE` via
     `claim_execute_attempt`) before any CRM write. A partial unique index allows
     only one execute-mode `in_progress` attempt per proposal. Concurrent callers
     and orphaned `in_progress` rows fail closed without a second POST/PATCH,
     because a prior process may already have written to the CRM. No time-based
     lock expiry and no silent claim takeover.
  20. Create duplicate/recovery identity (`_same_identity`) uses stable name and
     billing address fields only. Phone is ignored so blank proposed phones cannot
     miss an otherwise exact CRM account.
- Test suite on this branch: **165 passed**, fixture-based, no live POST/PATCH.
- Live site `bellhavenseniorliving.com` still NXDOMAIN; fixture HTML covers scrape.
- Real-world limit: execute mode is implemented and tested with fake sessions
  only. It has not been run against the assessment API.

## Open questions

- Whether a `lifetime_revenue` of exactly zero means "no revenue history". Until the
  data settles it, positive AR with zero revenue routes to human review via the
  `ZERO_LIFETIME_REVENUE_MEANS_NO_HISTORY` constant in `chow.py`.
