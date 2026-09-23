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
- Branch 2 `feat/scrape-and-match` is open as [PR #2](https://github.com/royho1/bellhaven-sync/pull/2).
  **Do not merge until Codex is clean on the latest commit (no P1/P2).**
- Latest Branch 2 fixes:
  1. Unit designators require a whole-word match and a real separator before the
     unit id, so street names like Stevens/Steele are not stripped as "Ste".
  2. Match edge costs are lexicographic tuples `(tier, similarity_penalty,
     facility_idx, account_rank)` so deterministic index tie-breaks can never
     outweigh a better name similarity (or tier).
  3. Prior: max-cardinality min-cost global assignment with intrinsic ambiguity
     preserved; listing HTML reused during enrichment; house-number-required
     usable location; punctuated unit stripping; urls-only never complete.
- Test suite on this branch: **90 passed**, fixture-based, no network required for
  scraper/matcher tests.
- Live site `bellhavenseniorliving.com` still NXDOMAIN; do not treat a failed live
  scrape as a product bug.
- Next after a clean merge of PR #2: `feat/proposals-and-review`.

## Open questions

- Whether a `lifetime_revenue` of exactly zero means "no revenue history". Until the
  data settles it, positive AR with zero revenue routes to human review via the
  `ZERO_LIFETIME_REVENUE_MEANS_NO_HISTORY` constant in `chow.py`.
