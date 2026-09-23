# Scheduling

The recurring job is read-only. It runs:

`scrape → CRM GET → match → proposals → SQLite`

It does not approve proposals and it does not call the CRM write path.

## Script

`scripts/run_scheduled_sync.sh` resolves the repo directory, runs the venv
interpreter, and appends stdout/stderr to `data/logs/`. The only Python command
inside it is:

```bash
python -m bellhaven_sync.cli sync
```

Logs under `data/` stay on the machine. They are gitignored.

## Example cron

This timing is an example so the job is easy to install by hand. It is not a
required business cadence. The tool does not install cron for you.

```cron
# Example only: Monday 06:00 local time. Edit the path. Do not point this at apply.
0 6 * * 1 /absolute/path/to/bellhaven-sync/scripts/run_scheduled_sync.sh
```

## Human workflow

1. The scheduled command stores a new reconciliation run.
2. Open the local review UI and approve or reject.
3. Plan writes with `python -m bellhaven_sync.apply_cli`.
4. Write only when `DRY_RUN=false` and `--execute` are both set.

Approval in SQLite is not application. A scheduled run will never turn an
approval into a CRM write.
