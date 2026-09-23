# bellhaven-sync

CRM reconciliation for Bellhaven Senior Living. The system scrapes Bellhaven's public
website, reads the Clipboard CRM, proposes conservative corrections, and applies only
what a human approves.

Nothing is written to the CRM without an explicit approval. The scheduled job is
read-only by construction: it cannot reach a write method.

## Status

Phases 0-2 are merged (`schema-discovery`, `scrape-and-match`). Branch 3 adds
proposal generation, SQLite persistence, `sync`, and a local Flask review UI.
**Branch 3 performs no CRM writes.** Applying approved proposals is Branch 4.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # then paste your token into .env
```

`.env` is gitignored. The API token is read from the environment only. It is never
committed, never logged, and never written into a snapshot or a document: everything
that could carry it passes through `redact()` first.

## Commands

```bash
# Read-only: confirm the token, page through every account, print the observed
# schema, and save a local snapshot under data/snapshots/.
.venv/bin/python -m bellhaven_sync.cli discover

# Read-only: scrape Bellhaven's public site (listing + sitemap + facility pages).
# Raises a completeness blocker when the union is short of the homepage claim.
.venv/bin/python -m bellhaven_sync.cli scrape

# Read-only: scrape + CRM GET + match + generate proposals into SQLite.
# Never POSTs or PATCHes the CRM.
.venv/bin/python -m bellhaven_sync.cli sync

# Local review UI. Approve/reject updates SQLite only; never calls the CRM.
.venv/bin/python -m bellhaven_sync.cli serve
```

## Proposal / review workflow

1. Run `sync` to scrape the site, read CRM accounts, match, and persist proposals
   under `data/bellhaven_sync.db`.
2. Run `serve` and open `http://127.0.0.1:5055`.
3. Filter by status / action type, then Approve or Reject.
4. Decisions stay in SQLite. Branch 4 will apply only approved items.

## Tests

```bash
.venv/bin/python -m pytest
```

Tests never touch the network. HTTP is stubbed with a fake session that only
implements `get`.

## Safety model

- `bellhaven_sync/crm_client.py` is GET-only and stays that way.
- All POST and PATCH capability will live in `bellhaven_sync/apply.py`, which the
  sync path never imports. `tests/test_write_boundary.py` walks the transitive import
  graph and fails if that ever changes.
- The CHOW rule (change of ownership) is isolated in `chow.py` so it can be read
  and verified in one screen.
- Branch 3 (`sync` / `serve` / proposals / store / review UI) never writes to the CRM.

## Docs

- `docs/schema-findings.md`: what the live API actually returns, observed rather than
  assumed, since the OpenAPI spec leaves the account schema empty.
