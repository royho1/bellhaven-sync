# bellhaven-sync

Local reconciliation for Bellhaven Senior Living. The tool scrapes the public
website, reads the Clipboard CRM, proposes conservative corrections, and writes
only what a person has approved and then explicitly executed.

Scheduled runs stay read-only. Nothing in the sync path can POST or PATCH.

The public site `bellhavenseniorliving.com` does not currently resolve in DNS.
Scrape behavior is covered by fixture HTML. A failed live scrape is not a
product bug until the site resolves.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # then paste your token into .env
```

`.env` is gitignored. `CLIPBOARD_API_TOKEN` is read from the environment only.
It is never committed, logged, or written into a snapshot. Anything that might
carry it goes through `redact()` first.

`DRY_RUN` defaults to true.

## Workflow

1. **Discover** the live account schema (read-only):

   ```bash
   .venv/bin/python -m bellhaven_sync.cli discover
   ```

2. **Scrape** the public site into a local facility list:

   ```bash
   .venv/bin/python -m bellhaven_sync.cli scrape
   ```

3. **Sync** scrapes, reads the CRM, matches, and stores proposals in SQLite.
   This command never writes to the CRM.

   ```bash
   .venv/bin/python -m bellhaven_sync.cli sync
   ```

4. **Review** in the local UI. The server binds loopback only
   (`127.0.0.1`, `localhost`, or `::1`). Approve and reject update SQLite and
   require the page's CSRF token.

   ```bash
   .venv/bin/python -m bellhaven_sync.cli serve
   ```

   Open `http://127.0.0.1:5055`.

5. **Plan writes.** This prints the plan and performs zero POST/PATCH calls:

   ```bash
   .venv/bin/python -m bellhaven_sync.apply_cli
   ```

6. **Execute writes** only when both gates are open. `--execute` while
   `DRY_RUN` is still true is refused.

   ```bash
   DRY_RUN=false .venv/bin/python -m bellhaven_sync.apply_cli --execute
   ```

   Optional: `--run-id` chooses a reconciliation run (default is the latest)
   and `--proposal-id` applies one approved proposal from that run.

7. **Schedule** the read-only sync. See `docs/scheduling.md`. The sample cron
   line is an example, not a required cadence, and it is not installed for you.

## What apply will write

Only approved proposals of these types, and only after a fresh CRM read still
matches the values that were reviewed:

| Action | CRM call |
| --- | --- |
| `update_fields` | PATCH the approved name, address, care type, or phone fields |
| `reparent` | PATCH exactly `{"parent_id": "..."}` |
| `create_account` | POST a new child with `created_by_candidate=true` |
| `chow_create_and_link` | POST a new account, then PATCH the old account with only `chow_current_account` |

Review-only types (`review_ambiguous`, `review_duplicate`,
`review_stale_or_missing`, `review_chow_ambiguous`, `review_inactive_account`,
`review_care_type`) never write, even if marked approved.

If a reviewed field changed in the CRM, apply stops that proposal and asks for
a new sync plus review. It does not edit the proposal.

CHOW saves the new account id before linking the old account. If the link
fails, the next execute reuses that id and does not POST again. If the old
account is already linked to that id, apply treats the CHOW as done. If it
points somewhere else, apply stops.

Approval status stays `approved`. Application results live in
`application_attempts` (planned, in progress, applied, blocked, or failed).

## Architecture / safety decisions

- `crm_client.py` is GET-only and stays that way. POST and PATCH exist only in
  `apply.py`.
- `python -m bellhaven_sync.cli sync` cannot import `apply.py`. The write
  command is a separate module, `apply_cli`.
- The scheduled shell script calls `cli sync` only.
- `tests/test_write_boundary.py` checks both the import graph and that no other
  module issues POST/PATCH.
- Two gates sit in front of a write: `DRY_RUN=false` and `--execute`.
- POST is not retried. A timeout can mean the account was created.
- The token is registered for redaction as soon as settings are loaded.

## Tests

```bash
.venv/bin/python -m pytest
```

Tests never call the live CRM. HTTP is stubbed.

## Docs

- `docs/schema-findings.md`: what the live API actually returns.
- `docs/scheduling.md`: the read-only cron example and the human apply step.
