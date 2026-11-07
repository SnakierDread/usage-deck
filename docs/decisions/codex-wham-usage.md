# Codex usage reopening — ratified November 7, 2026

Authority: architect card `t_ad8bf9bc`; consumer: builder `t_f5c963d0`.
Base inspected: `8e8b4de`. This decision supersedes the old Codex-only opacity
policy, not other provider policies. This is design authorization, not a claim
that the adapter is implemented, installed, or supported by a public API SLA.

## Approval terms (normative; copy verbatim to the builder card)

1. **Approve narrowly scoped credential egress.** Only `openai-codex` may send its
   existing selected `AdapterContext.credential` OAuth access token as a Bearer
   to `GET https://chatgpt.com/backend-api/wham/usage`. No other provider's
   credential, refresh token, ID token, cookie, browser session, credential file,
   or arbitrary header may be forwarded. Add exactly
   `AllowedTarget("https", "chatgpt.com", 443, ("/backend-api/wham/usage",))`
   to that adapter's guarded context, not a wildcard/global grant or a target
   derived from a JWT, config override, response, or browser input. Reject query,
   fragment, URL userinfo, redirects (even same-origin), alternate ports, sibling
   paths and hosts. Existing TLS, 2-second timeout, 1-MiB response bound,
   concurrency/deadline, no-retry, snapshot and cache rules remain in force.
   Preserve stdlib transport; a 200 from the scout's HTTP client does not
   guarantee the production transport will succeed. Do not add HTTP/2, httpx,
   browser impersonation, cookies, challenge bypasses or fallback endpoints.
2. Request headers are `Authorization`, `Accept: application/json`, a fixed
   honest `User-Agent: HermesAgent/usage-deck`, `originator: hermes-agent`,
   `ChatGPT-Account-ID`, and optional `x-openai-internal-codex-residency` only.
   Decode only the selected access token's `https://api.openai.com/auth` object:
   `chatgpt_account_id` supplies the account header;
   `chatgpt_data_residency` with fallback `chatgpt_compute_residency` supplies
   residency. Decoding is not signature verification or entitlement proof.
   Header claim strings must be nonempty printable ASCII without whitespace or
   control characters, at most 256 characters; absent residency is omitted.
   Missing/malformed account claim, malformed token or invalid nonempty residency
   means no request: opaque/source_unavailable. Do not accept account/residency
   from another pool row, merged sibling credential, response or user input.
   Never log/serialize these headers or claims; include private routing values
   in the masking context. This is the sole exception to the previous blanket
   'never send JWTs/claims anywhere' wording. OpenAI platform billing/admin
   endpoints remain forbidden for Codex OAuth.
3. **Use a real credit unit, not count or USD.** Add `credit` to `UsageMeter.unit`
   and `chatgpt_wham_usage` to `METER_SOURCES`; keep schema_version=1. Extend all
   canonical validators, source confidence ceilings, frontend formatters and
   schema/acceptance fixtures, then regenerate staging (never hand-edit staged
   duplicates). Unit display is `credits`, never `$`, tokens, messages or count.
   This scoped schema/UI/staging work is part of t_f5c963d0; for this card only,
   §7's backend/frontend ownership split permits these required coordinated
   edits by its builder. Generic cardinality would hide the provider's explicit
   `unit:"credit"`; the small additive schema change is preferable to ambiguity.
4. Emit **one** meter only for a supported
   `spend_control.individual_limit` with `source="workspace_spend_controls"`
   and `unit="credit"`:
   - id=`workspace_spend`; label=`Workspace spend limit (monthly)`;
   - value=`used`; limit=`limit`; remaining=`remaining`;
   - unit=`credit`; scope=`provider_account`; source=`chatgpt_wham_usage`;
   - period=`{start:null,end:null,label:"monthly_workspace"}`;
   - confidence=`exact`; observed_at=the refresh snapshot's UTC observation
     timestamp (the clock is captured once); resets_at=valid `reset_at` epoch
     seconds converted to UTC RFC3339, otherwise null. A present reset must be
     a finite nonnegative integral JSON number or decimal numeric string,
     representable as a UTC RFC3339 timestamp; booleans are invalid.
   Monthly identifies the recon-established workspace policy, not a calendar
   boundary inferred from plan, reset_after_seconds or the dashboard picker.
   Parse finite JSON numbers and decimal numeric strings; reject booleans,
   NaN/Infinity, malformed/blank strings, negative used/limit/remaining. Null or
   absent numeric fields stay null; never synthesize remaining, zero or limit.
   Invalid present numeric/reset fields yield malformed_response. Missing/null
   reset is allowed; a valid reset at or before observed_at suppresses the
   expired meter (opaque/source_unavailable), never resets usage to zero.
   Emit the meter if at least one of used/limit/remaining is non-null; the card
   is ready for any non-null observation, including zero or limit-only data.
   All numeric fields absent/null, absent individual_limit, or unrecognized
   source/unit gives opaque/source_unavailable with meters=[]; a wrong-shaped
   non-null individual_limit is malformed_response. A missing/null
   spend_control is unavailable; a non-null non-object is malformed_response.
   Do not use rounded used_percent/remaining_percent to overwrite observations
   or create a second meter. A progress bar may use value/limit only under §5's
   same-scope/period conditions; no bar for null/zero limit. No clamp of values,
   history manufacture, account aggregation, or currency conversion.
5. **No optional credits-balance meter in this implementation.** Ignore credits,
   model_usage, rate_limit*, promo, response identity and plan_type. Their
   accounting relation, periods and supported shapes are not established by
   this single fixture. Never emit upstream email/user_id/account_id, raw JWT,
   response plan/model fields, routing claims, or raw response/debug bags.
   Existing local sanitized plan metadata and public UsageDeck account IDs are
   unchanged; no response identity becomes an inventory ID. Exact means a
   directly observed credit quantity for this seat's workspace spend control,
   not a provider-wide subscription balance or a seat-disjoint fleet total.
6. **No OAuth refresh or credential mutations.** This overrides the builder
   card body's suggestion to invoke refresh_codex_oauth_pure. Preserve
   `auth_status=available|missing` as credential presence only; **do not add
   rejected**. A 401 yields state=error, confidence=opaque, meters=[],
   error.code=auth_required, retryable=false, reason=permission_denied;
   auth_status remains available when a credential was present. A 403 or 404
   yields opaque/permission_denied, meters=[], no alternate-route attempt.
   Other HTTP/transport/JSON failures use existing fixed safe DeckError tables;
   no upstream text, headers or identities in errors. Missing token makes no
   request and remains opaque/credentials_missing. Successful supported data
   carries debug.source=chatgpt_wham_usage and per-meter Exact badges; unavailable
   responses never promote confidence just because the endpoint is allowlisted.
   Reuse one request per snapshot; probe does not spend a separate request.
7. Verify downstream in scratch homes with injected transport: exact URL/header
   egress, malformed/hostile routing claims and missing credentials make no
   request; redirects/other origins/paths/ports are denied; success, zero,
   limit-only/null/malformed/nonfinite/negative numeric cases; reset parsing and
   expiry; unknown source/unit/other plan shapes; 401/403/404/429/5xx/timeouts;
   no refresh and unchanged credential files; no duplicate fetch or history;
   cross-seat/cross-profile routing isolation; all wire enum copies and source
   ceilings; canonical/staged packaging hashes; UI credits, ready/opaque/error
   states and removal of the obsolete blanket Codex-opaque explanation.
   Assert secrets and raw identity absent from DTOs, logs and caches. Do not
   repeat live probes or touch production for this card; this authorization is
   not a rollout instruction. Builder opens a reviewable PR; humans/contracts
   retain merge and production authority.

## Reopening evidence: recorded, not re-probed

Scout card `t_7dfdeb27` captured a read-only request to the exact approved GET
route. Its original artifact is
`/root/.hermes/profiles/scout/cache/scratch/artifacts/route2_wham_usage_200.json`.
It contains HTTP headers followed by the JSON body, despite its `.json` suffix.
The original status is **HTTP 200**, with response Date
**November 7, 2026, 16:14:40 GMT** (`2026-11-07T16:14:40Z`). No new live request
was made for this decision. The original stays private and is not committed.

The complete body, recursively sanitized for email/user_id/account_id and their
values, is committed separately at
[`../evidence/codex-wham-usage-200.sanitized.json`](../evidence/codex-wham-usage-200.sanitized.json).
All three identities are literal `[REDACTED]`; other observed body values and
nulls are preserved, not a synthetic sample. Response header/request IDs and
credentials are not copied. This is a point-in-time observation of one
spend/credit-based account, not evidence that every subscription has this shape.
The scout also reported `/backend-api/usage` returned 404; that path is not an
allowlisted alternative. No password, session mint or refresh permission follows.

Header claim names were checked read-only against local
`agent/codex_headers.py:54–80`; the current UsageDeck presence-only auth enum
and card assembly were checked against `plugins/usage_deck/models/dto.py` and
`service.py:352–396`. This decision deliberately corrects the proposed `count`
unit and nonexistent `rejected` auth enum rather than silently inventing either.
