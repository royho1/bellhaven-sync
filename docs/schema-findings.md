# Schema findings (Phase 0, read-only)

Everything below was observed by calling the live API with `python -m
bellhaven_sync.cli discover`, not read from the OpenAPI spec, which leaves the
account request and response schemas as empty objects. No POST or PATCH request
was made. Counts come from a full read of 121 accounts.

## Auth and envelope

- `GET /me` returns `{"candidate": "...", "message": "Token OK. ..."}`, which is
  enough to confirm the bearer token works.
- `GET /accounts` returns an object, not a bare list:
  `{"data": [...], "page": 1, "page_size": 50, "total": 121}`.
- Paging works as documented: `page` and `page_size`. Three pages at the default
  size of 50 returned all 121 accounts.

## Account fields

Eighteen fields, all present on every record:

| field | type | notes |
| --- | --- | --- |
| `account_id` | str | 18-character identifier, for example `001UKEFGADQ8YCZ4YM` |
| `name` | str | always set |
| `parent_id` | str | set on 95 of 121; `""` means no parent |
| `parent_name` | str | mirrors the parent's name, set on the same 95 |
| `billing_street` | str | set on 115 |
| `billing_city` | str | set on 115 |
| `billing_state` | str | set on 115, two-letter |
| `billing_zip` | str | set on 115 |
| `care_type` | str | set on 115 |
| `status` | str | `Active` (112) or `Inactive` (9) |
| `phone` | str | always set |
| `lifetime_revenue` | int | always present, never null |
| `outstanding_ar` | int | always present, never null |
| `chow_current_account` | str | `""` on all 121 today |
| `duplicate_of_account` | str | `""` on all 121 today |
| `note` | str | `""` on all 121 today |
| `created_by_candidate` | bool | `False` on all 121 today |
| `updated_at` | str | `2026-09-22 03:25:10Z` style, space separated, `Z` suffixed |

The single most important structural fact: **the API never returns null. Unset
fields come back as the empty string.** A blank-unaware summary reports every
field as fully populated, which is why `fields.is_set()` exists and why
`schema_probe.is_blank()` drives the populated/unset counts.

The identifier is `account_id`, not `id`.

## Enumerations

- `status`: `Active` (112), `Inactive` (9).
- `care_type`: Skilled Nursing (42), Assisted Living (36), Memory Care (22),
  Independent Living (15), unset (6).
- `billing_state`: OH (53), PA (22), MI (21), IN (18), unset (6), CO (1).

## Hierarchy

- 26 accounts have no parent. Six of them have children, and all six carry a
  `(Parent Account)` suffix in the name.
- Maximum depth observed is 1, so this is a flat parent/child model, not a tree.
- No `parent_id` points at a missing account.
- Parent accounts and their child counts: Juniper Point Healthcare (30),
  Bellhaven Senior Living (29), Stonebridge Eldercare (25), Cedar Trail
  Communities (5), Harborview Care Group (5), Millstone Health Partners (1).

### Bellhaven parent identification

Exactly one account matches: `0015QAPLGS3FVYEEEM`, named
`Bellhaven Senior Living (Parent Account)`, with no parent of its own. It is
unambiguous, so the resolver will not need to stop and ask on this data.

The naming detail that matters: the real name carries a `(Parent Account)`
suffix, so name normalization has to strip that marker before comparing against
`bellhaven senior living`. Selection still requires an exact normalized match
plus an empty `parent_id`, never a child count.

## Financial fields, and what they mean for CHOW

- `lifetime_revenue`: integer, never null, never negative. 74 accounts are zero,
  47 are positive, maximum 173000.
- `outstanding_ar`: integer, never null, never negative. 110 are zero, 11 are
  positive, maximum 12400.
- Every account with `outstanding_ar > 0` also has `lifetime_revenue > 0`. There
  are zero accounts with positive AR and zero revenue.

That last point resolves the open question conservatively. Because the ambiguous
combination does not occur in the data, `ZERO_LIFETIME_REVENUE_MEANS_NO_HISTORY`
can stay at its safe default (positive AR with zero revenue goes to human
review) without blocking any real case. Nothing observed here establishes that a
zero means "no revenue history", so the code will not assume it.

## Bellhaven accounts needing attention

Thirty-one accounts mention Bellhaven. Twenty-six sit under the Bellhaven parent
already. The rest are the interesting ones, and they map onto both CHOW branches:

- `Bellhaven of Tiffin` (Cedar Trail, revenue 84000, AR 12400) and
  `Bellhaven of Marietta` (Cedar Trail, revenue 51250, AR 3800) have positive AR,
  so they take the CHOW path: new account under the Bellhaven parent, then a
  single-key `chow_current_account` patch on the old account.
- `Bellhaven Crossings of Lima` (Harborview, revenue 47000, AR 0) and
  `Bellhaven Meadows of Findlay` (no parent, revenue 22000, AR 0) have zero AR,
  so they are direct re-parents.

Findlay being parentless in the CRM lines up with it being missing from the
website's `/communities` listing, which is exactly the case the scraper's
completeness check has to protect.

Three accounts sit under the Bellhaven parent without a Bellhaven name
(Chesterton Senior Commons, Riverbend Manor Care Center, Sunny Acres Retirement
Home). These are evidence for a human, not an automatic action.

## Operational notes

- `created_by_candidate` is `False` everywhere today. Accounts this tool creates
  will presumably flip it to `True`, which gives the apply path a cheap way to
  recognize its own writes during a resume.
- `chow_current_account`, `duplicate_of_account`, and `note` are empty across the
  board, so any value in them later is something this tool put there.
- No rate limit is documented, and none surfaced across the handful of sequential
  requests a full discovery run makes (one `/me` plus three pages). Requests stay
  sequential with explicit timeouts rather than throttled to an invented number.
