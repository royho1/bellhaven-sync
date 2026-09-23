"""Minimal local Flask UI for reviewing reconciliation proposals.

Approve/reject only updates SQLite. This app never calls the CRM.
"""

from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, url_for

from .proposals import STATUS_APPROVED, STATUS_PENDING, STATUS_REJECTED, STATUS_VALUES
from .store import ProposalStore, StoredProposal

PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Bellhaven Sync Review</title>
  <style>
    :root {
      --ink: #1c2430;
      --muted: #5b6775;
      --line: #d7dde5;
      --bg: #f6f8fa;
      --card: #ffffff;
      --ok: #1f6b3a;
      --bad: #8a1f1f;
      --accent: #245b8a;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      color: var(--ink);
      background: linear-gradient(180deg, #eef3f8 0%, var(--bg) 220px);
    }
    header {
      padding: 1.5rem 1.75rem 1rem;
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,0.85);
    }
    h1 { margin: 0 0 0.35rem; font-size: 1.45rem; }
    .sub { color: var(--muted); margin: 0; }
    main { padding: 1.25rem 1.75rem 2.5rem; max-width: 1100px; }
    form.filters {
      display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: end;
      margin-bottom: 1.25rem;
    }
    label { display: grid; gap: 0.25rem; font-size: 0.85rem; color: var(--muted); }
    select, button {
      font: inherit; padding: 0.45rem 0.7rem; border-radius: 6px;
      border: 1px solid var(--line); background: white;
    }
    button { cursor: pointer; }
    button.primary { background: var(--accent); color: white; border-color: var(--accent); }
    button.ok { background: var(--ok); color: white; border-color: var(--ok); }
    button.bad { background: var(--bad); color: white; border-color: var(--bad); }
    .card {
      background: var(--card); border: 1px solid var(--line); border-radius: 10px;
      padding: 1rem 1.1rem; margin-bottom: 0.9rem;
    }
    .meta { display: flex; flex-wrap: wrap; gap: 0.75rem 1.25rem; color: var(--muted); font-size: 0.9rem; }
    .meta strong { color: var(--ink); font-weight: 600; }
    h2 { margin: 0.55rem 0 0.4rem; font-size: 1.05rem; }
    .reason { margin: 0.4rem 0 0.75rem; }
    table.diff { width: 100%; border-collapse: collapse; font-size: 0.92rem; }
    table.diff th, table.diff td {
      text-align: left; vertical-align: top; padding: 0.35rem 0.4rem;
      border-top: 1px solid var(--line);
    }
    table.diff th { width: 18%; color: var(--muted); font-weight: 600; }
    pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, monospace; font-size: 0.86em; }
    .actions { display: flex; gap: 0.5rem; margin-top: 0.85rem; }
    .empty { color: var(--muted); padding: 1.5rem 0; }
    code { font-family: ui-monospace, monospace; font-size: 0.86em; }
  </style>
</head>
<body>
  <header>
    <h1>Bellhaven Sync Review</h1>
    <p class="sub">Local SQLite decisions only. This UI never writes to the CRM.</p>
  </header>
  <main>
    <form class="filters" method="get">
      <label>Status
        <select name="status">
          <option value="">all</option>
          {% for value in statuses %}
          <option value="{{ value }}" {% if value == status %}selected{% endif %}>{{ value }}</option>
          {% endfor %}
        </select>
      </label>
      <label>Action
        <select name="action_type">
          <option value="">all</option>
          {% for value in action_types %}
          <option value="{{ value }}" {% if value == action_type %}selected{% endif %}>{{ value }}</option>
          {% endfor %}
        </select>
      </label>
      <button class="primary" type="submit">Filter</button>
    </form>

    {% if not items %}
      <p class="empty">No proposals match these filters. Run <code>python -m bellhaven_sync.cli sync</code> first.</p>
    {% endif %}

    {% for item in items %}
    <article class="card">
      <div class="meta">
        <span>ID <strong>#{{ item.id }}</strong></span>
        <span>Run <strong>{{ item.run_id }}</strong></span>
        <span>Action <strong>{{ item.action_type }}</strong></span>
        <span>Status <strong>{{ item.status }}</strong></span>
        <span>Confidence <strong>{{ item.confidence }}</strong></span>
      </div>
      <h2>
        {% if item.facility_url %}{{ item.facility_url }}{% endif %}
        {% if item.account_id %} · account <code>{{ item.account_id }}</code>{% endif %}
        {% if not item.facility_url and not item.account_id %}Proposal #{{ item.id }}{% endif %}
      </h2>
      <p class="reason">{{ item.reason or "No reason recorded." }}</p>
      <table class="diff">
        <tr><th>Current</th><td><pre>{{ item.current_json }}</pre></td></tr>
        <tr><th>Proposed</th><td><pre>{{ item.proposed_json }}</pre></td></tr>
        <tr><th>Evidence</th><td><pre>{{ item.evidence_json }}</pre></td></tr>
      </table>
      {% if item.status == 'pending' %}
      <div class="actions">
        <form method="post" action="{{ url_for('set_status', proposal_id=item.id) }}">
          <input type="hidden" name="status" value="approved">
          <input type="hidden" name="return_status" value="{{ status }}">
          <input type="hidden" name="return_action_type" value="{{ action_type }}">
          <button class="ok" type="submit">Approve</button>
        </form>
        <form method="post" action="{{ url_for('set_status', proposal_id=item.id) }}">
          <input type="hidden" name="status" value="rejected">
          <input type="hidden" name="return_status" value="{{ status }}">
          <input type="hidden" name="return_action_type" value="{{ action_type }}">
          <button class="bad" type="submit">Reject</button>
        </form>
      </div>
      {% endif %}
    </article>
    {% endfor %}
  </main>
</body>
</html>
"""


def _pretty(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _view_item(item: StoredProposal) -> dict:
    return {
        "id": item.id,
        "run_id": item.run_id,
        "action_type": item.action_type,
        "account_id": item.account_id,
        "facility_url": item.facility_url,
        "status": item.status,
        "confidence": item.confidence,
        "reason": item.reason,
        "current_json": _pretty(item.current_values),
        "proposed_json": _pretty(item.proposed_values),
        "evidence_json": _pretty(item.evidence),
    }


def create_app(db_path: Path | str) -> Flask:
    store = ProposalStore(db_path)
    app = Flask(__name__)
    app.config["BELLHAVEN_DB_PATH"] = str(db_path)

    @app.get("/")
    def index():
        status = (request.args.get("status") or "").strip() or None
        action_type = (request.args.get("action_type") or "").strip() or None
        if status and status not in STATUS_VALUES:
            status = None
        items = [_view_item(p) for p in store.list_proposals(status=status, action_type=action_type)]
        return render_template_string(
            PAGE,
            items=items,
            status=status or "",
            action_type=action_type or "",
            statuses=sorted(STATUS_VALUES),
            action_types=store.action_types(),
        )

    @app.post("/proposals/<int:proposal_id>/status")
    def set_status(proposal_id: int):
        new_status = (request.form.get("status") or "").strip()
        if new_status not in {STATUS_APPROVED, STATUS_REJECTED, STATUS_PENDING}:
            return ("Invalid status", 400)
        store.set_status(proposal_id, new_status)
        return redirect(
            url_for(
                "index",
                status=request.form.get("return_status") or None,
                action_type=request.form.get("return_action_type") or None,
            )
        )

    return app


def run_server(db_path: Path | str, *, host: str = "127.0.0.1", port: int = 5055) -> None:
    app = create_app(db_path)
    app.run(host=host, port=port, debug=False)
