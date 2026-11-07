# UsageDeck MVP contract — v1

Status: **ratified; backend and frontend implementation released**. Amended by card `t_04a4354d` (builder-authored): §9 source upgrade, §10 machine view, §11 badges; **§12 (ratified design decision `t_64a95086`, implemented by `t_3e2434b4` on top of PR #6) supersedes every conflicting statement in §1.1 and §§9–11, except that the later Codex-only amendment below overrides §12.4 for its one exact route**. Superseded text is kept below as historical context and carries an explicit `Superseded by §12.x` pointer. §12 ratifies a design; it is not a code-review PASS of PR #6 and grants no install permission; merging follows the auto-merge-on-PASS convention in `docs/decisions/merge-convention-auto.md` (user-ratified 2026-11-07; it supersedes the earlier human-only merge gate).
Codex-only amendment ratified by architect `t_ad8bf9bc` on November 7, 2026 (later than §12): §9.2 and `docs/decisions/codex-wham-usage.md` reopen one verified usage route and override §12.4's no-probe / no-allowlist-exception decision for exactly that route (GET `https://chatgpt.com/backend-api/wham/usage`, selected-profile effective credential only, §12.5); every other Codex route stays forbidden. The adapter is implemented and merged (PR #9). This does not re-ratify unrelated pending deltas.
Owner: architect. Branch: `t_22e02c08`. Wire version: `1`.
This document governs backend and frontend together. Examples below are contract examples, not live probe results. `docs/recon-providers.md` is evidence, not an API specification.

## 0. Ratified integration decision

The brief mandates `/api/usage-deck/*`. The installed Hermes dashboard unconditionally mounts a plugin router at `/api/plugins/<manifest.name>/*`; its isolated plugin host uses that namespace too. A plugin cannot remove this prefix. Evidence: `hermes_cli/web_server_dashboard.py:1239–1247`, forwarding at `:1019–1043`, at local Hermes revision `b200f5bc60e31588ba2cb60e288029c4a54ba5fa`. Adding `/api/usage-deck` inside the exported router would create an incorrect double prefix, not the requested routes.

**Approved decision:** use `/api/plugins/usage-deck/*`, retaining `plugins/usage_deck` as the source directory and `usage-deck` as manifest identity. Approval was recorded by `dashboard` on design card `t_0ce0532d` on November 6, 2026: “I approve native /api/plugins/usage-deck”. This supersedes `/api/usage-deck/*` in the original brief and implementation card bodies. It preserves the host authentication, consent and profile isolation gates without host modifications. No top-level alias, second server or gate bypass is authorized or required.

All route suffixes, DTOs and behaviors below are final. `API_BASE` is one shared frontend constant fixed to `/api/plugins/usage-deck`. No speculative alias, absolute origin, or credential in its URL.

A second grounding correction is settled here, not left to workers: the CLI installs agent packages containing `plugin.yaml` plus `dashboard/manifest.json`, not a bare root dashboard manifest. Preserve the requested root files as canonical sources and produce a deterministic dashboard staging copy (§7). Evidence: `hermes_cli/plugins_cmd_inspect.py:57–87`; existing `plugins/kanban/dashboard/manifest.json:1–14`.

## 1. Adapter Registry Contract

### 1.1 Inventory and identity

Discovery is local, read-only, scoped to the dashboard-selected profile using the host's request-scoped profile helper. Never mutate process environment per request. Resolve the active home through Hermes helpers, not `~/.hermes` string concatenation. Allow only host-validated profile names; no arbitrary filesystem paths in request parameters.

Read credential pools profile-first, with global fallback **per provider only when the profile has zero pool entries for that provider**. A nonempty profile pool replaces the global provider pool; do not combine their accounts. This matches `hermes_cli/auth.py:934–958`. Legacy `providers` entries are used only when no effective pool exists for that provider. Add configured primary/custom providers even without credentials. Use the host's configuration loader (including its YAML support), never a hand-written YAML parser. Do not scan other profiles or read `.env` wholesale (the **machine view** of §10 is the one exception: it reads other profiles' `auth.json` only; **bounded and clarified by §12.5**: same-OS-owner inventory only, never another home's config/`.env`/analytics, never another home's credentials for any outbound request, symlinked `profiles/` parents refused). Resolve only the credential references required by a discovered adapter, through host helpers. Never refresh OAuth, rotate credentials, persist pool health, or invoke an inference request.

One SubscriptionCard per effective pool account; one per configured endpoint without a pool account. A configured endpoint and pool row referring to the same provider/base URL are one inventory item, not two. Dedupe exact duplicate stored row IDs and normalized endpoints, not different accounts just because their provider matches. Do not dedupe different stored row IDs by hashing their secret values.

Preserve stored `provider_id` literally (including case/punctuation for custom names); registry aliases are lookup-only and must not rewrite response IDs. Canonical built-in IDs: `nous`, `openrouter`, `openai-codex`, `anthropic`, `ollama`. A configured custom name with no stored ID gets `custom:<configured-name>` exactly as configured. Match configured names case-insensitively only if the match is unique. Never infer provider identity from a model-name prefix.

`account_id` = `acct_` plus first 24 lowercase hexadecimal characters of SHA-256 over UTF-8 JSON of `[provider_id, normalized_endpoint, public_row_id_or_ordinal]` (compact separators, ensure_ascii=True). Public row ID is a stored non-secret row `id`; otherwise a one-based source-order ordinal. Endpoint normalization removes trailing `/`, userinfo, query and fragment; lowercase scheme and hostname, preserve path/port. IDs may change if an anonymous account's source order changes; never derive them from tokens, emails or JWT subjects. `endpoint_id` uses the same algorithm with `ep_` and `[provider_id, normalized_endpoint]`. Card `id` equals `account_id`. No secrets appear in ID inputs.

Display account labels as `Account 1`, `Account 2`, etc. in source order, not emails or provider account IDs. Plan metadata may be extracted from known plan-name claims in existing JWTs locally, without verifying their authority; allowlist short strings and set `plan_source=unverified_claim`. Never send JWTs anywhere except the narrowly approved Codex access-token bearer and private routing claims to the exact §9.2 target, and never use a plan claim to infer a quota, billing cycle, account identity or entitlement. *(Narrowed by §12.5: the unverified JWT account+user claims of canonical `openai-codex` are used as a private display-dedup heuristic only; plan claims still never imply entitlement.)* Unsupported claims yield null plan. No live fleet addresses or credential assumptions in code.

### 1.2 Protocol and orchestration

The following is a normative Python typing sketch, not a requirement to import Pydantic. All methods are synchronous; the host runs orchestration in a worker thread. One adapter instance per immutable private AdapterContext and refresh snapshot.

```python
class ProviderAdapter(Protocol):
    def probe(self) -> ProbeResult: ...
    def get_usage(self, window: Literal[7, 30, 90]) -> UsageResult: ...
    def get_health(self) -> HealthStripEntry | None: ...
    def get_confidence(self) -> ConfidenceLabel: ...

REGISTRY: dict[str, type[ProviderAdapter]]
```

AdapterContext (never serialized) contains provider/account/endpoint identity, read-only resolved credential access, profile-scoped AnalyticsReader, injected HTTP transport, clock, and inventory metadata. No ambient credential lookup inside an adapter. Factories and injected test doubles must accept `AdapterContext` as their single constructor argument.

`ProbeResult`: `supported: bool`, `reachable: bool|null`, `checked_at: UTC timestamp`, `capabilities: list[quota|usage|metrics|session_analytics]`, `reason: reason-code|null`, `error: DeckError|null`. Unsupported access is a normal result, not an exception. Probe validates capability, not the existence of an API key. An offline endpoint is distinguishable from unsupported telemetry.

`UsageResult`: `meters: list[UsageMeter]`, `series: list[UsageTimeSeries]`, `confidence: ConfidenceLabel`, `reason: reason-code|null`, `error: DeckError|null`. Empty list means no supported meters, not zero consumption. `get_confidence()` returns the safe default for the currently successful primary source, initially opaque; it may not upgrade on credential presence alone. Per-meter and per-series confidence always override this default downward or distinguish a separate source. Health does not promote billing confidence.

Order: construct → probe once → get_usage(window) → get_health() for local endpoints → get_confidence(). Each call reuses the snapshot; no repeated network request just to obtain a badge. An adapter must be callable independently by a unit test; it lazily probes if no snapshot exists. Ordinary provider failures become DeckError/ProbeResult, never crash sibling cards; programming/invariant errors become a sanitized internal_error at the coordinator boundary and are observable server-side without raw payloads. No raw exceptions in DTOs.

Registry resolution: exact built-in ID → configured custom/ollama endpoint adapter → SessionAnalyticsAdapter. `custom:*` and configured ollama-style endpoints select `SelfHostedAdapter`; unknown cloud IDs select fallback. No remote auto-discovery scans and no dynamic Python imports based on an ID.

### 1.3 Required adapter policy

| Class / registry entry | Allowed data sources in MVP | Meaning / fallback |
|---|---|---|
| NousAdapter / nous | local inventory and AnalyticsReader | No documented verified quota capability in recon; quota opaque. Never scrape Portal HTML or guess `/api/usage`. |
| OpenAICodexAdapter / openai-codex | local inventory, safe plan claim, AnalyticsReader; HTTPS GET `https://chatgpt.com/backend-api/wham/usage` only (§9.2) | Exact credit quantities only for the supported workspace spend-control shape; otherwise opaque/error under §9.2. Codex OAuth is allowed only at that fixed usage target, never at OpenAI platform billing/admin endpoints. API org billing is not ChatGPT subscription quota. |
| AnthropicAdapter / anthropic | local inventory, safe plan claim, AnalyticsReader, **captured unified rate-limit headers (read from a local file, §9.1)** | OAuth/ordinary inference credentials do not establish org analytics or subscription quota access. Quota opaque unless a header capture exists for the card's credential; then exact session/weekly utilization. No SDK or admin endpoint guessing, no request made to obtain headers. |
| OpenRouterAdapter / openrouter | HTTPS `https://openrouter.ai/api/v1/key`; AnalyticsReader | Read documented numeric `usage`, `limit`, `limit_remaining` from `data`; USD, API-key scope. Exact only for returned finite values and matching limit period. Missing or unauthorized values opaque, never inferred from plan. |
| SelfHostedAdapter / custom endpoints, ollama | configured origin's `/metrics`, `/v1/models` or `/api/tags` for health; AnalyticsReader separately | Successful supported metrics exposition gives self_hosted health, not a token quota. No metrics gives opaque health confidence, even if models are reachable. |
| SessionAnalyticsAdapter / all other IDs | **only** AnalyticsReader mapped to GET `/api/analytics/usage` | Session analytics only; quota opaque, profile-only series estimated. No provider HTTP, config mutations, `/api/analytics/models`, DB reads or model-prefix attribution. |

OpenRouter mapping is fixed: if at least one allowed finite numeric field is returned, create one meter `id=api_spend`, `label=API credential spend`, `unit=usd`, `scope=api_credential`, `source=openrouter_key`, `value=data.usage`, `limit=data.limit`, `remaining=data.limit_remaining` (missing fields null), confidence exact, observed_at equal to fetch time, and period `{start:null,end:null,label:unknown}`. Do not compute percentages or period boundaries for this MVP meter; `usage` need not share the configured limit's reset interval. If all three values are absent/null, meters is empty and quota is opaque; successful explicit zeros remain observed values. Reject negative usage/limits, but retain a finite negative remaining balance as observed overspend; this is the sole signed balance field. Do not parse arbitrary budget arrays into extra meters in v1.

OpenRouter `/credits` is deliberately **not** used in MVP: it is account-wide credit information, may require different privilege, and cannot safely be summed across unknown shared accounts. No management-key flow or account-wide credit consumption mislabeled as per-key subscription usage. Never use its totals to manufacture 7/30/90-day history. Future admin analytics adapters require their own credential opt-in and API mapping; not implicit in this contract.

Recon is a point-in-time failed/successful probe record. A 401 using an invalid credential does not prove an endpoint deprecated; model discovery does not establish token usage; no model metrics were observed for the sampled local endpoints. Its exact labels for local `/metrics` are superseded by self_hosted here. Anthropic's documented analytics SDK is not a dependency permission or proof that this fleet's OAuth grants access.

### 1.4 AnalyticsReader, attribution and time

Reuse the host route's service in-process through an injected wrapper implementing `read_usage(days, profile)`; this is a logical read of `/api/analytics/usage`, not unauthenticated HTTP back into the dashboard. Map window to `days`. No additional SQL, DB opens, or auth forwarding. Import the host analytics module lazily; unavailable/corrupt analytics becomes analytics_unavailable, not a plugin import failure.

Verified host response has `daily` (day, input_tokens, output_tokens, estimated_cost, actual_cost, sessions, api_calls), `totals`, `by_model`, `period_days`. It has **no daily provider/account attribution** (`hermes_cli/web_routers/analytics.py:84–159`). Consequently MVP always returns one profile-scoped session series, once, for unfiltered usage. It never clones that total into each provider/account. A provider-filtered request returns `series: []`, `reason: attribution_unavailable` until a verified attributed source exists. Unknown providers must still invoke their session fallback, but must not mislabel profile totals as provider usage. Subscriptions may link to profile-wide analytics; they do not display it as account spend or subscription allowance.

Host `daily.day` uses the host local calendar timezone and a rolling days cutoff. Preserve source dates and `timezone=server_local`; do not relabel them UTC or promise complete UTC days. No fabricated zero-filled gaps; chart breaks for missing dates. `period.start` and `period.end` describe the reader's UTC rolling interval; `bucket=day` is the source grouping, `coverage=partial` because the first day is partial and telemetry can be incomplete. Use a single injected clock per refresh. Estimated cost maps from `estimated_cost`; ignore `actual_cost` for the MVP chart instead of silently mixing billed and estimated totals. Nulls stay null; observed zero stays zero. If a row has no numeric usage observations it is not a measured zero.

### 1.5 Transport, cache and security

Only GET. Fixed cloud origin/path allowlist. Cloud targets: OpenRouter `AllowedTarget("https", "openrouter.ai", 443, ("/api/v1/key",))`; Codex `AllowedTarget("https", "chatgpt.com", 443, ("/backend-api/wham/usage",))`, granted only to the selected openai-codex context (§9.2). No wildcard or fallback Codex paths; reject query/fragment on that target. Local requests must match inventory-configured http(s) origin; endpoints are never accepted from query strings. Derive `/metrics` and health paths from origin, not by blindly appending to `/v1`. Reject URL userinfo and credential-bearing query/fragment for transport; do not log the rejected raw URL. No redirects, including same-origin; no credentials copied to another origin. The sole newly ratified credential egress is the selected Codex access token and private routing headers to the exact chatgpt.com target above, not api.openai.com billing or configured proxies. Verify TLS. Do not send inference credentials to `/metrics` by default; use only a separately configured metrics credential if one already exists. No new connection/credential UX in MVP.

Custom-endpoint credentials resolve with precedence inline `api_key` > `env_secret(key_env)` (`key_env`/`api_key_env` of a `custom_providers` entry, read through the inventory source's `env_lookup`; only the variable's *value* is ever used and it is registered as a redaction secret). A `key_env` whose variable is unset yields no credential and **no request at all** (`credential_expected`): card `auth_status: missing`, reason `credentials_missing`, health `unknown`. An entry with no credential and no `key_env` (e.g. Ollama) is still probed anonymously; an anonymous 401 there is reported as `credentials_missing` (never as a rejected credential, and with no `auth_required` error). `auth_required` / `auth_status: rejected` are reserved for a request that actually carried a bearer and got 401.

Inject a stdlib HTTP implementation (urllib, redirect handler disabled); finite per-request timeout 2 seconds, maximum response 1 MiB, maximum four concurrent provider workers, overall refresh deadline 8 seconds. On deadline pending items become timeout; workers may finish within their transport timeout but may not overwrite a later snapshot. No in-request retry; refresh provides retry. Bound Prometheus parsing to known simple numeric samples: CPU percent and memory bytes only when names/units match an allowlist. Never render arbitrary labels, exemplars, process paths or raw exposition. Reject NaN/Infinity/negative byte counters; average per-core CPU samples, do not add percentages.

Coalesce requests and cache immutable sanitized snapshots for 30 seconds, keyed by validated profile/home, provider/account identity and window. Capture context into each worker; never share credentials across homes. Changing profile invalidates frontend state and starts new requests; discard late responses with an epoch counter. No raw credential-bearing HTTP payload persistence. Browser/API: Cache-Control: no-store. UI displays checked_at and stale status; no automatic stale-error-success substitution. Manual Refresh invokes normal endpoints (no unsupported refresh parameter); may hit the 30-second cache. Explain this in tooltip. Initial load then poll every 60 seconds while visible; stop when hidden.

## 2. Data Model (DTOs)

Pydantic-style schema notation below is normative JSON shape, including required fields and nullability. Implement stdlib frozen dataclasses plus explicit validators/construction factories and JSON conversion; do not add Pydantic, requests, provider SDKs or Prometheus libraries. FastAPI and SDK imports are host-owned integration only and must be lazy for the bare-Python import acceptance test. Omitted optional fields are **not** interchangeable with null: every listed field must be serialized. Unknown fields are rejected/dropped before serialization. No JSON NaN or Infinity; unknown is null, not zero. Identifiers are strings; timestamps UTC RFC3339 `YYYY-MM-DDTHH:MM:SSZ`; date buckets `YYYY-MM-DD`.

```text
ConfidenceLabel = exact | estimated | opaque | self_hosted
CardState = ready | error | opaque | empty
Reason = unsupported | credentials_missing | permission_denied | attribution_unavailable
       | no_observations | metrics_unavailable | source_unavailable | null

DeckError {
  code: invalid_query | provider_not_found | auth_required | permission_denied
      | timeout | upstream_unavailable | malformed_response | inventory_unavailable
      | analytics_unavailable | internal_error,
  message: string,                 # fixed safe public text, never upstream text
  retryable: boolean,
  provider_id: string|null,
  account_id: string|null
}

Period {
  start: timestamp|null, end: timestamp|null, # exclusive end; both null if unknown
  label: string                            # safe enum-like label, e.g. lifetime, rolling_30d, unknown
}

UsageMeter {
  id: string, label: string,
  value: number|null, limit: number|null, remaining: number|null,
  unit: usd | count | percent | bytes | credit,
  scope: api_credential | provider_account | endpoint | profile,
  period: Period,
  confidence: ConfidenceLabel,
  source: openrouter_key | session_analytics | prometheus | anthropic_headers | chatgpt_wham_usage | none,
  observed_at: timestamp|null
}

SubscriptionCard {
  id: string, provider_id: string, account_id: string,
  provider_label: string, account_label: string,
  kind: cloud | self_hosted,
  plan_name: string|null,
  plan_source: unverified_claim | provider | none,
  auth_status: available | missing | rejected,  # available = presence only, NOT validity; missing = no credential, none sent; rejected = a credential WAS sent and the provider answered 401
  endpoint_id: string|null,
  state: CardState, confidence: ConfidenceLabel,
  reason: Reason,
  meters: list[UsageMeter],
  checked_at: timestamp, stale: boolean,
  error: DeckError|null,
  debug: { adapter: string, capabilities: list[string],
           source: string, sanitized: true }
}

UsagePoint {
  day: date,
  input_count: integer|null, output_count: integer|null,
  estimated_usd: number|null, sessions: integer|null,
  api_calls: integer|null,
  confidence: ConfidenceLabel
}

UsageTimeSeries {
  id: string, provider_id: string|null, account_id: string|null,
  scope: profile | provider | account,
  confidence: ConfidenceLabel,
  source: session_analytics,
  period: Period, window_days: 7|30|90,
  bucket: day, timezone: server_local, coverage: partial | complete,
  points: list[UsagePoint],
  checked_at: timestamp, stale: boolean, error: DeckError|null
}

HealthStripEntry {
  endpoint_id: string, provider_id: string, display_name: string,
  status: healthy | degraded | offline | unknown,
  confidence: ConfidenceLabel,
  metrics_available: boolean,
  latency_ms: number|null,
  cpu_percent: number|null, memory_bytes: integer|null,
  checked_at: timestamp, stale: boolean,
  reason: Reason, error: DeckError|null
}
```

Health status: metrics success plus successful expected health JSON => healthy; health reachable but metrics unsupported/failed => degraded; all attempted requests fail connection/timeout => offline; invalid/unconfigured endpoint or nonconnection failures without valid health evidence => unknown. Metrics success but health request failure => degraded, with self_hosted confidence retained for the successful metrics observation. Health latency measures the selected successful health request, else successful metrics request, else null; a failed request is not zero latency. Missing CPU/memory remains null even with a valid empty exposition. Non-local adapters return no health row. At most one row per endpoint ID even if accounts share an endpoint.

Wire deltas to the notation above (additive; `schema_version` stays `1`): `Holder`, `SubscriptionCard.holders`, `UsageMeter.resets_at`, `METER_SOURCES += anthropic_headers`, `/subscriptions data.view`. Normative text in §9-§10.
Codex delta (`t_ad8bf9bc`, §9.2): `UNITS += credit`, `METER_SOURCES += chatgpt_wham_usage`; auth_status remains presence-only `available|missing` (no `rejected`). All canonical and staged validators, frontend unit/source tables and test/acceptance schema copies must agree. Regenerate staging rather than hand-editing it. The expanded exact-source ceiling applies only to supported numeric observations, not every successful HTTP response.

### 2.1 Secret masking at DTO construction

Use one shared safe factory before any DTO exists, caches, logs, drawer data or response serialization. Allowlist fields above; no raw payload bag. Recursively redact values under any field name containing (case-insensitive) `key`, `token`, `secret`, `password`, plus authorization, cookie, credentials, bearer, jwt, access, refresh. Replacement is literal `[REDACTED]`; recurse through lists/maps and reject non-JSON objects. Do not retain suffixes or fingerprints. This intentionally forbids wire names like `input_tokens` and `api_key_id`; map allowed numeric analytics fields into `input_count`/`output_count` **before** the DTO factory. Do not allow a token-count exception that can also leak credential fields.

Additionally scrub all displayed strings and nested values for the exact nonempty secret values in the current private context (including values under generic names), credential-looking prefixes/JWT/Bearer patterns, and URL userinfo/query/fragment. Reject a configured display string that contains such a value; use `Provider`/`Account N` instead. Reason/error text comes only from fixed local tables. Arbitrary upstream strings never become labels. Construction must not retain raw private context or reference mutable raw dicts. Debug drawer contains only `SubscriptionCard`'s already-sanitized JSON, not a second debug endpoint or network response. Frontend uses text rendering, never innerHTML. Unit-test redaction under generic names, mixed-case keys, nested arrays and exception text.

## 3. API Routes and envelopes

Required physical endpoints (approved in §0):

- GET `/api/plugins/usage-deck/subscriptions`
- GET `/api/plugins/usage-deck/usage?window=7|30|90&provider=<id>`
- GET `/api/plugins/usage-deck/health`

Export a relative router (`/subscriptions`, `/usage`, `/health`) with **no prefix**; the host supplies `/api/plugins/usage-deck`. A test harness mounts it at the same API_BASE; actual host namespace must be verified in integration QA, not merely a standalone TestClient. The old `/api/usage-deck/*` namespace is not exposed.

Common optional query `profile` is passed through the host profile validation/scope mechanism. Optional `view=machine|profile` (default `machine`, §10) on all three endpoints. Omitted = dashboard active profile. All endpoints inherit the dashboard auth/consent/profile gate; no public bypass and no provider credentials in requests. Unknown query parameters and repeated single-value parameters => 400 invalid_query. Window defaults to 30; accept only the literal strings `7`, `30`, `90` (not 07, floats or arbitrary days). Provider absent means unfiltered; empty string or >256 characters/control characters => 400; percent-decode once through the host, match exact discovered provider ID. Unknown inventory provider => 404 provider_not_found (this does **not** mean a discovered provider with unknown adapter should fail).

Every endpoint's plugin-owned success response (HTTP 200):

```json
{"ok":true,"data":{},"error":null,"meta":{"schema_version":1,"request_id":"uuid","generated_at":"2026-11-06T00:00:00Z","partial":false,"errors":[]}}
```

`data` by suffix:

```text
/subscriptions: { cards: list[SubscriptionCard], inventory_state: empty|detected, view: machine|profile }  # view added by §10
/usage: { window_days: 7|30|90, provider_id: string|null,
          series: list[UsageTimeSeries], reason: Reason }
/health: { entries: list[HealthStripEntry] }
```

Failure response (plugin-owned 400/404/503/500): `ok=false`, `data=null`, `error=DeckError`, same `meta` shape. 400 malformed query, 404 provider not discovered, 503 unreadable/malformed inventory or unavailable required analytics, 500 invariant failure. Successful inventories with zero entries are 200 empty, not 404. Missing inventory files mean empty discovery, not unreadable inventory; report malformed JSON/config without leaking paths or contents. Host 401/403 gates may return their native error shape before plugin code runs; frontend treats HTTP status as authoritative and never assumes plugin envelope on host failures.

Per-provider partial failures are HTTP 200, `meta.partial=true`, `meta.errors` populated and matching card/health errors; do not 500 a whole grid for an upstream 401. Unsupported capability/unknown attribution is a nonerror opaque result and does not alone make partial=true. Whole-profile unavailable analytics is 503 for `/usage` only; subscription inventory and health remain usable. No credentials required means no credential wall, not that the dashboard authentication is optional.

Return all inventory cards/health rows without pagination; sort cards by `(provider_id, account_id)`, health by `(provider_id, endpoint_id)`, series by id and points by day ascending. No cursor/count/has_more fields. No cross-account provider-total meter unless source proves disjoint identity and periods; MVP does not. Provider filtering may match several accounts but does not sum duplicated account-wide balances. No account filter in v1.

Example empty success: `data={"cards":[],"inventory_state":"empty","view":"machine"}` (`view` is always serialized, §10/§12.5). Example unknown adapter card: `state=opaque`, `confidence=opaque`, `reason=unsupported`, `meters=[]`, `error=null`; the profile chart is separately estimated. Source-specific units or missing limits are never filled from UI guesses.

## 4. UI State Machine

Three independent zones, one screen: subscription grid, usage chart (7/30/90 picker + provider picker), self-hosted health strip. A failure in one zone never erases successful siblings. Use only host SDK's React/components/hooks/fetchJSON; inherit selected profile and auth behavior. Plugin tab `/usage-deck`, label UsageDeck, position `after:models`; unsupported positioning may fall back to host's default insertion order. Accessible status text, keyboard drawer controls, focus restoration and aria-expanded; status is not color alone.

Global `first_run`: entered on initial mount/profile change before successful inventory retrieval. Immediately fetch inventory and show loading skeleton, no login/provider-key form. Successful empty inventory stays first_run and shows “No providers detected in this profile” with Refresh and host configuration guidance, without opening a credential input. Successful nonempty inventory transitions to `connected` regardless of telemetry capability or credentials. Connected → first_run when later inventory is empty or profile changes; network errors do not fabricate an empty inventory. Host 401/403 shows dashboard access error, not provider credential collection. Session-only chart may render even in first_run.

Per-card `loading` is frontend-only; server returns terminal CardState. Loading → error for retryable/transport/malformed/auth-source errors; → opaque for unsupported/missing-permission telemetry or no quota capability; → empty only for supported telemetry explicitly reporting no observations; → ready when at least one supported non-null measured value exists. Explicit observed zero is ready, never empty. Healthy self-hosted cards with no billable meter remain opaque while health is independently useful. Unsupported missing quota alongside an available meter does not suppress that meter; source badges remain metric-specific.

Ready/error/opaque/empty → loading on refresh/profile change. During a same-profile refresh keep prior sanitized values with stale indicator and aria-busy; a failed refresh transitions error, keeps prior data visibly stale, and removes its progress meter. Never retain prior-profile data. Error → loading only via polling/Refresh; late old-window/profile results are discarded. Opaque has “Usage unavailable”/“Session analytics only” copy, not a grey zero-percent meter. Empty has “No observations for this period.” Loading skeleton has no numeric value/badge pretending to be observed data. Expand drawer in any loaded terminal state; display only sanitized DTO.

Chart: window defaults 30; preserve selection within mount, reset profile data before refetch. Unfiltered means profile-wide session analytics; subtitle explicitly “This Hermes profile only — not subscription allowance.” Show input/output counts and estimated USD as separate charts or distinct axes with units; never sum counts and dollars. A filtered unavailable series says “Provider attribution unavailable from session analytics.” Do not plot fake zero lines, extrapolate resets, or join gaps. Error and empty chart states are separate from inventory states. No chart dependency: use React + native SVG. Health strip uses text/status icon, latency, and measured CPU/memory only when non-null; no quota bar from CPU percentages.

## 5. Confidence Rules

Wire `self_hosted` is canonical, display badge is **Self-hosted** (resolves brief's hyphenated prose vs DTO enum). Other badge text: exact → **Exact**, estimated → **Estimated**, opaque → **Opaque**. Tooltip must name source, scope and observed time. Every number/meter/chart and health entry carries its own source badge; never rely solely on a card header badge.

- exact: only directly reported numeric quantity for a known scope/period. OpenRouter key balance/usage can qualify; it is not an exact subscription percentage. Anthropic's reported 5h/7d utilization from captured headers also qualifies (§9.1): it is the provider's own number for a named rolling window, shown with the capture time and never extrapolated. Codex workspace spend controls reported in credits qualify under §9.2, source `chatgpt_wham_usage`; not USD, message counts, other subscription windows or fleet totals.
- estimated: session-local accounting, estimated spend or incomplete attribution/coverage. No estimated spend presented as a provider invoice or subscription allowance.
- opaque: no verified measurement. No numeric quota/percent/reset rendering; null is not zero. Credentials and unverified plan claims cannot promote confidence.
- self_hosted: operational metrics exposed by a configured endpoint's valid `/metrics`. It is a source category, **not** a higher accuracy tier than estimated/exact. Local token history, when available from session accounting, stays estimated separately.

`get_confidence()` maps 1:1 to a card's primary source badge. A secondary meter always uses its own confidence. For a combined same-unit same-scope value all sources must qualify; any estimated input lowers exact to estimated, any missing/opaque input makes the complete total unavailable (do not present subtotal as total). Self_hosted cannot be combined with a billing quantity; split components. No arithmetic over currencies/scopes/periods. MVP avoids cross-account aggregate meters altogether.

A progress meter requires finite value ≥0 and finite limit >0, same source/scope/unit/period, non-opaque confidence, and explicit documented period alignment. Preserve raw ratio and show overspend text for >100%; cap visual fill only, not amount. Unlimited/missing/zero limits => numeric value if valid, no progress meter. OpenRouter lifetime usage with period-unknown limit cannot form a quota bar. Remaining must be a source value or a subtraction of matching documented limit/value with identical scope/period; otherwise null. No reset estimate from calendar or plan name.

## 6. No-runtime-deps constraint

Backend core: Python stdlib only (`dataclasses`, `typing`, `enum`, `json`, `hashlib`, `urllib`, `concurrent.futures`, `time`, `datetime`, `re`). Use already-shipped Hermes config/profile/analytics services. HTTP router integration may reuse the dashboard's existing FastAPI; do not declare/install additional backend dependencies. `import plugins.usage_deck.plugin_api` must succeed without FastAPI/Hermes imports being available: expose `create_router()` for lazy host wiring and set `router` only when host FastAPI is present (None in a bare environment). Actual dashboard requires non-null router and exercises create_router, never treats bare import as integration success. Core adapters/DTOs must import independently without installed Hermes.

Frontend: existing host SDK and dashboard build toolchain only; no CDN, external fonts, telemetry, charts package, provider SDK, runtime package download or copied React runtime. Plain SDK IIFE may be committed directly like Kanban, avoiding a new build dependency. Use SDK.fetchJSON, not localStorage tokens or custom auth. Reuse host CSS variables discovered in the target build; do not invent required token names. Scope CSS to `.usage-deck`; use `var(...)` with inherited `currentColor`/transparent fallbacks, no hardcoded light/dark color palette. Manifest and distributable JS/CSS are checked in.

## 7. File Layout and ownership

```text
DESIGN.md                              architect only
 docs/recon-providers.md                 original recon evidence, unchanged
 plugins/__init__.py                    backend
 plugins/usage_deck/__init__.py          backend; no side effects
 plugins/usage_deck/plugin_api.py        backend; thin router, lazy integration
 plugins/usage_deck/adapters/            backend; protocol, registry, cloud/local/fallback
 plugins/usage_deck/models/              backend; DTOs, validators, redaction
 plugins/usage_deck/discovery.py         backend (machine view: `discover_machine`, §10)
 plugins/usage_deck/captures.py          backend; capture schema 2: scoped reader + locked, confined writer (§9.1 → §12.3)
 plugins/usage_deck/observer.py          backend; opt-in agent observer subscribed to provider_ratelimit_headers (§12.1)
 plugins/usage_deck/service.py           backend; snapshots/orchestration
 plugins/usage_deck/tests/               backend tests; QA may add new test files
 plugins/usage_deck/manifest.json         frontend; canonical dashboard manifest
 plugins/usage_deck/dist/index.js        frontend; canonical SDK bundle
 plugins/usage_deck/dist/style.css       frontend
 plugins/usage_deck/plugin.yaml          frontend; installable agent metadata
 plugins/usage_deck/dashboard/           generated integration staging, frontend owns
```

Backend workers must not edit frontend-owned files. Frontend owns deterministic packaging: copy canonical manifest/dist to dashboard/; copy backend plugin_api/adapters/models/discovery/service and necessary __init__ files when available, preserving their relative imports. Dashboard plugin_api is file-loaded under a synthetic module name, so bootstrap one private stable package namespace pointing at its bundled directory using stdlib importlib, then import only its own sibling package; no global `sys.path` mutation, cwd imports, or requirement that source repo remain on disk. Stage must be rebuilt after backend commits. Contract tests must import both the source package and staged plugin via the real host loader. Do not hand-edit two copies; verify staged/canonical content hashes in packaging tests. No symlinks pointing outside the installed package.

Canonical manifest fields: name=`usage-deck`, label=`UsageDeck`, version=`0.1.0`, icon=`Activity`, tab.path=`/usage-deck`, tab.position=`after:models`, entry=`dist/index.js`, css=`dist/style.css`, api=`plugin_api.py`; description is a short plain-text summary. Agent `plugin.yaml` declares name `usage-deck`, version `0.1.0`, description, author and no added dependencies; validate against installed CLI, not an invented slot field. The directory name is `usage_deck`, wire/manifest identity `usage-deck`.

Shared branch requirement is `t_22e02c08`, created from main by this design task. Workers must start from this design commit, not recreate/reset the branch from main. Parallel work uses separate task branches/worktrees off this base and coordinated cherry-picks into the shared integration branch by dispatch; never simultaneously checkout the shared branch in multiple worktrees, force-reset, stage all files, or merge another worker's unverified changes. If dispatcher insists on sequential shared-branch execution, finish backend first then frontend packaging. No worker merges PRs or changes production. Dispatch owns final integration ordering and installation.

## 8. Contract verification required downstream

Backend fixtures: two accounts under one provider; profile pool shadows global; empty/missing/malformed files; config-only custom endpoints; unknown cloud provider; safe plan-only claim; malformed JWT; duplicate endpoint; credentials missing; HTTP 401/403/404/429/5xx/timeouts/redirect/oversize/invalid JSON/NaN; successful/absent/invalid metrics; mismatched limits; mixed attribution; source unavailable. Assert exact envelope/schema/null values, no fake zeros, no duplicate account billing totals, no credential mutations, and explicit finite timeouts.

Secret tests: seed unique sentinels in nested config/auth/HTTP/error strings; recursively scan **all DTO fields, debug data, envelopes, captured logs and cache outputs**, including generic field names and accidental URL/userinfo. Assert numeric count aliases survive but raw token/key fields do not. Run against a scratch HERMES_HOME, never rewrite real credentials.

Frontend fixtures: every terminal card state and loading transition; empty/nonempty inventory; partial failure; profile/window switch with out-of-order completion; null/zero/over-limit/unlimited progress behavior; badges per value; drawer text escaping; keyboard access; hidden-tab polling; chart attribution unavailable; health without metrics. Smoke-test all three zones in the real dashboard SDK, and install via `hermes plugins install ./plugins/usage_deck` only after staged packaging exists and owner-approved route namespace is settled. Live probes are read-only, opt-in through existing configured credentials; report nonresponsive endpoints honestly rather than assert fleet totals from old recon.

Design ratification checklist: seven required sections present; recon preserved and corrections explicit; schemas and masks consistent; physical host mounting contract verified against source; §0 namespace choice approved; docs committed on requested branch; commit ID handed to both implementation cards. Runtime host installation and route smoke tests remain downstream implementation/QA obligations, not evidence claimed by this design card. No feature implementation or external design review is part of this card.

## 9. Source upgrade: Anthropic header meters, Codex decision (delta, card t_04a4354d)

Status: implemented; the delta text is builder-authored and **pending architect ratification**. It amends §1.3, §2, §3 and §5 only where stated here.

### 9.1 Anthropic: session and weekly utilization from captured headers

> **Superseded by §12.1–§12.3 where it conflicts.** A Hermes hook and an opt-in UsageDeck observer now exist (§12.2); the capture file is **schema 2**, scoped by holder; v1 files are refused; "nothing records these headers" and "the plugin itself never calls `record_headers`" below describe the original PR #6 state only. The meter mapping table below is **ratified unchanged** (§12.5).

Evidence: recon `t_7dce2222` (`recon_report.md`). Anthropic OAuth responses carry `anthropic-ratelimit-unified-{5h,7d}-{utilization,reset,status}` plus `-representative-claim`, `-fallback-percentage`, `-reset`, `-overage-*`. Utilization is a fraction (`0.0184` = 1.84 %, may exceed `1.0`); `*-reset` is epoch seconds. The recon cites public issues (anthropics/claude-code #12829, #55333) for the header names, not a live capture from this fleet.

Finding that shapes the design: **nothing in the Hermes source tree records these headers** (searched at revision `b200f5bc60e31588ba2cb60e288029c4a54ba5fa`). The seams the recon cites (`_VENDOR_RESET_HEADERS`, `nous_rate_guard._parse_buckets_from_headers`, `rate_limit_tracker.parse_rate_limit_headers`) read `x-ratelimit-*` and the per-bucket `anthropic-ratelimit-requests/tokens-reset` headers only, and the `post_api_request` hook payload carries no response headers. UsageDeck therefore does **not** make a request to obtain them (no inference call, no spend; §1.1/§1.5 unchanged) and only reads a capture file:

- Path: `<hermes root>/plugin-data/usage-deck/anthropic-ratelimit.json`, schema in `captures.py` (`schema_version: 1`; one capture per `{provider, credential_id}`; `credential_id` is the pool row's public `id`, never a token, email or fingerprint; `observed_at` is the time of the call that returned the headers; only allow-listed `anthropic-ratelimit-unified-*` header names with values up to 64 characters are loaded, everything else is dropped).
- `captures.record_headers()` is the write seam a future capture hook would call. The plugin itself never calls it. **Until some component records captures, every Anthropic card is opaque, exactly as before.** This is the honest default, not a bug.
- A capture file or entry flagged `"synthetic": true` is refused by the production reader, so test fixtures can never render as observations.

Meter mapping (`AnthropicAdapter`, `adapters/cloud.py`), one meter per window that has a valid utilization:

| field | value |
|---|---|
| `id` / `label` | `session_5h` "Session (5-hour window)" and `weekly_7d` "Weekly (7-day window)" |
| `value` / `limit` / `remaining` | utilization × 100 rounded to 4 dp (may exceed 100, never clamped) / `100` / `null` |
| `unit` / `scope` | `percent` / `provider_account` |
| `period` | `{start:null, end:null, label:"rolling_5h"\|"rolling_7d"}`: a documented rolling window, boundaries never computed |
| `confidence` / `source` | `exact` / `anthropic_headers` (new `METER_SOURCES` member) |
| `observed_at` | the capture's call timestamp: the freshness. Never the read time |
| `resets_at` (new) | the provider-reported reset instant, else `null` |

Never extrapolated: a window whose reported reset is already in the past no longer describes the current window, so it produces **no** meter (its utilization since the reset is unknown and is not shown as zero). If no window survives, the card is `opaque` with `reason=source_unavailable`. A capture for a credential that is not on the card is never used. Without a capture: `opaque`, `reason=unsupported`, `debug.source=local_inventory`.

UI: each meter carries its own `Exact` badge; the tooltip names the source ("Anthropic unified rate-limit headers captured from the last call"), scope, observed time and the provider's reset, and says the value is as of that call and never extrapolated. The row shows `Used`, `Resets`, `As of`, plus a progress bar (the rolling window is a known period; an over-100 % value is shown as over limit).

### 9.2 Codex: verified wham usage route reopened (ratified decision)

Architect card `t_ad8bf9bc` supersedes the earlier opaque/no-network decision on November 7, 2026. Its required reopening evidence is now satisfied: scout `t_7dfdeb27` recorded **HTTP 200** for GET `https://chatgpt.com/backend-api/wham/usage`, response Date `2026-11-07T16:14:40Z`. The complete sanitized observed body (email/user_id/account_id replaced with `[REDACTED]`) is `docs/evidence/codex-wham-usage-200.sanitized.json`. Provenance and the **normative approval terms** are in `docs/decisions/codex-wham-usage.md`; that document is part of this contract. No new probe, production write, refresh or credential mutation is authorized by this amendment.

Only the selected existing Codex access-token bearer plus same-token account/residency routing claims may leave the private context, and only for that exact guarded GET target (§1.5). OpenAI platform billing/admin, `/backend-api/usage`, old guessed paths, redirects, cookies, session minting, other origins and endpoint overrides remain forbidden. Missing or malformed routing claims do not trigger fallback calls. No arbitrary upstream identity or plan fields are emitted.

Mapping: `spend_control.individual_limit` with source `workspace_spend_controls`, unit `credit` produces one meter `workspace_spend`, label `Workspace spend limit (monthly)`, value/limit/remaining from the respective `used`/`limit`/`remaining` numeric observations; finite numeric strings are accepted. Unit **credit**, not count or USD; scope `provider_account`; source `chatgpt_wham_usage`; period `{start:null,end:null,label:"monthly_workspace"}`; confidence exact; observed_at is the immutable refresh snapshot observation time; resets_at is the valid reported `reset_at` UTC instant or null. Missing values remain null. No calendar start/end or usage history is invented. Deferred: credits-balance meter, model/rate-limit mappings, plan-specific alternatives and account aggregation. Unsupported shapes remain opaque/source_unavailable, invalid present values are malformed_response, expired observations are not rendered as current. See the decision for precise validation and error rules.

Credential presence remains auth_status `available|missing`: a 401 yields an auth_required error, not a new `rejected` enum and not refresh. The old Codex-always-opaque UI explanation must be replaced with source-aware ready/opaque/error states and credits display. Downstream tests must verify egress isolation, sanitized output, all schema/unit/source copies, transport failures, expiry and staged packaging. Further endpoints or schema shapes require a new architect decision and recorded sanitized evidence; successful reachability of this one account is not a general API guarantee.

Historical note: §12.4 (written before this amendment) held Codex at no-probe with an unratified candidate URL. That decision is superseded for this exact route only; its URL-provenance correction (`/backend-api` is part of the path) stands. In the machine view (§12.5) only a member of the SELECTED profile's effective pool may supply the Codex bearer and its same-token routing claims; a machine-only holder issues no request and supplies no credential.

### 9.3 The capture chain: three independent states (card `t_891857a7`)

Anthropic meters exist only when **all three** links below hold. Each link fails closed on its own, and a missing link is indistinguishable from "no capture yet" in the UI (the card is `opaque`); diagnose them in this order. Normative behaviour stays in §12.1–§12.3; this section states the chain and where each link lives.

| # | State | Lives in | Present when | If absent |
|---|---|---|---|---|
| 1 | **Core hook** | Hermes (`agent/provider_ratelimit_headers.py`, registered in `hermes_cli.plugins.VALID_HOOKS`) | the running Hermes build publishes `provider_ratelimit_headers` for a native `api.anthropic.com` OAuth response with a public pool row id | nothing is published; `observer.register()` sees the hook missing from `VALID_HOOKS` and subscribes nothing |
| 2 | **Plugin sink** | this repository: package `register(ctx)` → `observer.register` → `register_hook("provider_ratelimit_headers", observe)`, and `captures.record_headers` | the **installed** UsageDeck copy contains `observer.py` and the plugin is enabled for the *originating* profile (`plugins.enabled`) | the hook fires into no subscriber; no file is ever written. A build without the plugin-side `register` (the pre-observer PR #6 state) has exactly this symptom |
| 3 | **Opt-in flag** | the originating profile's `config.yaml`: `usage_deck.capture_anthropic_headers: true` (default off; only boolean `true` counts) | set for the profile that issued the request, not the credential holder and not the displayed profile | the observer returns before any write, request or log line; opt-out leaves existing observations untouched |

How the links compose at runtime: the core builds the payload and routes on `hermes_root` **in process only**; the observer checks that root and `execution_profile` against its own host context, reads the flag for that profile, re-sanitizes the headers and writes `<root>/plugin-data/usage-deck/anthropic-ratelimit.json` through `record_headers`, attributing the entry to the payload's `holder` + `credential_id` (a profile on the global fallback is attributed to the global holder, not to itself). `hermes_root` is never written to the capture file, a DTO or a log line (`tests/test_observer.py` asserts the stored file contains neither the key nor the root path).

Consequences that are easy to miss:

- Having the core hook and the flag without the installed plugin sink captures nothing (observed on this fleet's host on 2026-11-07: the core observer fired for an Anthropic OAuth call, but the deployed plugin had no `register_hook`, so no sink file appeared). Merging this repository is not the same as deploying it: the installed copy under `<root>/plugins/usage-deck/` must be refreshed.
- The flag is per originating profile. A call made by profile `builder` needs `builder`'s own `config.yaml` setting **and** the plugin enabled for `builder`; the root profile's setting does not cover it.
- Setting the flag alone never enables anything (§12.1); neither does installing the plugin.
- Plugin modules are loaded once per process. A long-running Hermes process (gateway, dashboard, kanban dispatcher) keeps whatever UsageDeck copy it loaded at start; refreshing the installed files does not reach it until that process restarts. Observed on 2026-11-07: with the observer-bearing copy installed, a `hermes chat -q` (which logged `gateway.run: inbound message`, i.e. ran in the already-running gateway, not in the CLI process) captured nothing, while a fresh process (`run_oneshot` after `discover_plugins()`, real OAuth call, root profile with the flag set) wrote a schema-2 entry for the global row. Restarting those processes is a deployment decision for the human operator, not something this repository does.

## 10. Machine view: aggregation across Hermes homes (delta, card t_04a4354d)

> **Amended by §12.5.** The *Identity* and *Card identity* bullets and *Merged card* below are superseded: JWT grouping needs BOTH account and user claims under `https://api.openai.com/auth` and applies only to canonical `openai-codex`; identical public row ids across different homes never merge; identity is compact ASCII JSON; capture references are scoped `(is_global, profile, row id)` tuples; another home's credentials are never used for outbound requests; a profile-restricted host principal is forced to the profile view.

The subscriptions grid and health strip now default to a **machine view**; the profile-scoped view of §1.1 remains behind a toggle.

- Query: `view=machine|profile` on `/subscriptions`, `/health` and `/usage`; default `machine`; anything else (or a repeat) is `400 invalid_query`. For `/usage` the value only decides which inventory validates `provider=`; usage series stay profile-wide session analytics (§1.4).
- Sources: the root `auth.json` (`holder` `global`) and every `<root>/profiles/<name>/auth.json`, each read **on its own** (no shadowing, no config). A profile that merely falls back to the global pool does not hold those accounts. Legacy `providers` entries count only for providers with no pool row in the same file. This **supersedes §1.1's "do not scan other profiles" for the machine view only**: it widens what one dashboard session can see to every profile on the same machine, by explicit card instruction. Safeguards: only real directories with valid profile names directly under `<root>/profiles` and non-symlink `auth.json` files are opened (nothing outside the Hermes root), at most 64 profiles, an unreadable store is skipped with a path-free log line (the viewed home's own store still fails with `inventory_unavailable`), and no tokens or claims ever reach a response.
- Config-derived items (custom endpoints, primary provider without credentials) come from the **viewed** profile's config only (host config loader) and are held by that profile (`global` when the viewed home is the root). They are dropped when a pooled account already covers the same provider (cloud) or provider and endpoint (self-hosted).
- Identity (private merge key, never serialized): (1) the JWT account identity `chatgpt_account_id` **plus** user id (`chatgpt_user_id` / `user_id` / `chatgpt_account_user_id`) from the `https://api.openai.com/auth` claim, so two seats of one workspace stay two accounts and a refreshed token of the same seat merges; (2) otherwise provider plus an identical **public pool row id** (so two api-key rows to one endpoint remain two accounts, as in the profile view); (3) otherwise no identity: the row is never merged across homes. Secrets are never compared or hashed to merge (§1.1 unchanged), so an opaque credential copied under a new row id shows twice.
- Card identity: `account_id`/`id` = `acct_` + SHA-256-24 of `[provider_id, endpoint, "machine:" + identity]`. The identity string is hashed, never emitted; JWT claims are read locally and never sent anywhere except the same-token private account/residency routing headers expressly authorized by §9.2. Machine ids differ from profile-view ids by design. Labels are `Account N` per provider in first-seen order (global, then profiles alphabetically).
- Merged card: first credential and plan claim found; secrets unioned for masking; capture lookup uses every public row id of the group.

Wire delta (additive, `schema_version` stays `1`; every field always serialized):

```text
SubscriptionCard += holders: list[Holder]       # machine view: homes holding the account, global first; [] in profile view
Holder { profile: string, is_global: boolean }  # profile matches ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$; "global" + is_global=true for the root
UsageMeter       += resets_at: timestamp|null
METER_SOURCES    += anthropic_headers
/subscriptions data += view: machine|profile    # the effective view; "profile" if the source cannot enumerate homes
```

Example title: `OpenAI Codex [go] — verifier` (plan claim is an unverified claim; the holders list comes from local `auth.json` presence, not validity). Fixtures and checks: `plugins/usage_deck/tests/test_machine_view.py`, scratch fixture `tests/fixtures/scratch_home/` (adds profile `verifier`), and the real-host harness machine section.

## 11. Confidence badges for the new claims

> **Wording updated by §12.5:** the Anthropic row now reads "No usable capture; enable the optional observer and use this account normally." (never promising an observation before an ordinary request returns eligible headers). The Codex row of the same wording update (a *source-derived candidate endpoint; not live-verified*, §12.4) is historical and superseded by the later Codex amendment: the Codex row below is the current one (§9.2).

| Rendered claim | Badge / treatment |
|---|---|
| Anthropic `Used`, `Resets`, `As of` | per-meter `Exact` (source `anthropic_headers`, ceiling `exact`; the UI lowers it to the lesser of claimed confidence and source ceiling) |
| Anthropic without a usable capture | card `Opaque`; tooltip and body say "No usable capture; enable the optional observer and use this account normally." (§12.5); nothing estimated |
| Codex | supported workspace credit observations: per-meter `Exact`, source `chatgpt_wham_usage`, unit displayed `credits`; otherwise source-aware opaque/error and fixed safe explanation (§9.2). Do not retain the superseded blanket no-live-response tooltip. |
| Plan `[go]` | unchanged: unverified claim, tooltip says so; not a measurement |
| Holders `— verifier` | local inventory fact: tooltip says "read from auth.json; presence only, not validated"; not a usage claim, so no confidence tier applies |

## Grounding references

Local Hermes checkout inspected for this design:
- `plugins/kanban/dashboard/manifest.json:1–14` and `dist/index.js:8–24,674–707`: real manifest and host SDK usage.
- `hermes_cli/web_server_dashboard.py:1019–1043,1239–1247`: isolated forwarding and mandatory plugin route prefix.
- `hermes_cli/plugins_cmd_inspect.py:57–87`: installation payload detection.
- `hermes_cli/auth.py:934–958`: per-provider profile-first pool resolution.
- `hermes_cli/web_routers/analytics.py:84–159`: actual analytics output/rolling days/local dates and lack of provider-scoped daily rows.
- `docs/recon-providers.md`: upstream observations; not proof of entitlement or automatic admin API access.

Official Hermes documentation index was retrieved and identifies “Extending the Dashboard” as the authoritative topic. Its page and the OpenRouter current-key reference were downloaded, but this run could not reliably extract their body text (extract backend unavailable; browser lacks libglib). Accordingly, the decisions above are grounded in the inspected local source and the upstream recon, not claimed as newly verified provider documentation. No new Anthropic analytics endpoint or SDK assertion is adopted. Local Hermes source revision inspected: `b200f5bc60e31588ba2cb60e288029c4a54ba5fa`.

## 12. Ratified amendment: opt-in Anthropic capture observer, scoped capture schema 2, scoped machine inventory (t_64a95086)

Status: RATIFIED design decision, November 6, 2026 (owner: architect). Applies to PR #6 at `30bc686811bfb634923f6743e5ec1ad56dc3602b`. Supersedes conflicting text in §1.1 and §§9–11; it is not a code-review PASS or permission to merge/install.

### 12.1 Decision and ownership

Choose **(b): opt-in agent-side plugin observer**, supported by a small Hermes core hook exposing only sanitized quota headers. Hermes owns extracting headers and binding request-local credential provenance; UsageDeck's agent plugin owns optional persistence through `captures.record_headers`. The dashboard remains a read-only consumer. Core must not import UsageDeck, know its storage schema, or write its files. No monkeypatching SDKs, wrapping globally shared clients, scraping logs, or adding inference requests.

The core addition is necessary infrastructure for (b), not option (a)'s direct core-to-UsageDeck writer. Existing `post_api_request` remains unchanged: do not expose arbitrary response headers or response objects there. A new narrowly typed hook avoids accidentally publishing cookies, request IDs, or authorization metadata to generic observers.

Consent: new profile-local setting `usage_deck.capture_anthropic_headers: true` explicitly enables the UsageDeck observer; absent/false disables it. Resolve it through the originating agent profile's host config loader, never the currently displayed dashboard profile or process-global environment mutation. Installing/opening the dashboard alone does not enable capture. No setting is changed on this host by this card. Opt-in permits storing quota observations from **already-authorized ordinary requests only**, not making additional requests, changing request headers/betas, refreshing credentials, or retrying. Opt-out stops writes; existing observations remain subject to reader freshness/reset rules. Explain local storage location and manual deletion in README.

### 12.2 Core/plugin interface (new, not claimed to exist today)

Hook name: `provider_ratelimit_headers`. Synchronous, observational, return value ignored, subscriber exceptions isolated. Register it in the host's hook name/dispatch metadata and document it alongside plugin lifecycle hooks. Do not forward this internal hook to shell hooks, outbound webhooks, UI events or generic payload logging.

Keyword payload, all fields required:

```text
schema_version: 1
provider: "anthropic"
hermes_root: str                 # absolute resolved root; private in-process routing only
execution_profile: {profile: str, is_global: bool}  # originating agent home; opt-in scope
holder: {profile: str, is_global: bool}             # selected credential's owning store
credential_id: str               # public pool row id used by THIS HTTP attempt
observed_at: str                 # UTC RFC3339 seconds, response headers receipt time
headers: dict[str, str]          # lowercase, sanitized allowlist below only
```

`holder` names the auth store that owns the selected row, NOT the execution profile when that profile used global fallback. Root holder is `{profile:"global",is_global:true}`; a profile literally named global is `{profile:"global",is_global:false}` and remains distinct. Profile-name grammar is DESIGN §10's grammar; row-id grammar/64-character bound is `captures.py:_ID_RE`. `hermes_root` is never persisted, serialized in DTOs, or logged. Validate the supplied root against the originating host context; no caller-selected path or HTTP input can reach this hook.

Capture actual provider/native Anthropic OAuth requests to TLS origin `https://api.anthropic.com:443` only (default port equivalent), not arbitrary custom Anthropic-compatible proxies, Bedrock, Vertex or API-key rows. Bind immutable `{root, holder, row id, OAuth/provider/origin eligibility}` when the request credentials/client are selected, before dispatch. Never read mutable `_credential_pool_entry_id` after the response arrives. Rotation, retries, nested requests and concurrent calls must retain each attempt's original provenance. If a public id or owning auth store cannot be established without guessing, **skip**; environment-only and legacy credentials without public row IDs stay opaque. No token hashing or guessed ids to fill this gap.

Use the existing raw Anthropic `on_response` path for successful streaming and non-streaming requests. Emit once per actual HTTP response with eligible headers, not once per stream chunk or again on aggregation. Transport errors without headers do not emit. Error responses may emit only if the same native callback exposes their actual headers and request provenance safely; otherwise skip them (no new error-path HTTP calls). Do not alter rate-limit/credits behavior or user-visible request success/failure.

Allowlist exactly the existing `captures.ALLOWED_HEADERS` suffixes under `anthropic-ratelimit-unified-`: `status`, `reset`, `representative-claim`, `fallback-percentage`, `5h-status`, `5h-reset`, `5h-utilization`, `7d-status`, `7d-reset`, `7d-utilization`, `overage-status`, `overage-disabled-reason`. Maximum 12 entries and 64 printable ASCII characters per value. Validate numeric values as finite, nonnegative numbers (resets additionally use `parse_reset_epoch`'s supported epoch range); status/claim/reason values must be bounded enum-like atoms `[A-Za-z0-9_.-]+`. Unknown names or invalid values are discarded independently; empty sanitized set means no event/write. Header validation occurs in core before dispatch and again in the sink; never persist arbitrary text merely because its header name is allowed. No authorization, cookies, body, token, email, model prompt, session ID or full request/response in this interface.

### 12.3 Capture storage and errors

Keep path `<hermes root>/plugin-data/usage-deck/anthropic-ratelimit.json`. **Bump capture schema to 2**, independently of HTTP wire version 1. Entry:

```json
{"provider":"anthropic","holder":{"profile":"example","is_global":false},"credential_id":"public-row-example","observed_at":"2026-11-06T19:00:00Z","headers":{"anthropic-ratelimit-unified-5h-utilization":"0.12"}}
```

Top-level shape remains `{"schema_version":2,"captures":[...]}`. Unique key is `(holder.is_global, holder.profile, provider, credential_id)`. Public row IDs are not guaranteed unique across auth stores. An entry must never supply another holder's card. Replace private inventory `row_ids` lookup with scoped references carrying this tuple; a merged card consults only the exact scoped references of its members. Profile view uses effective pool provenance, including global fallback. Machine view uses source-store provenance, not whichever store happened to be read first.

Write seam becomes `record_headers(path, provider, credential_id, headers, observed_at, *, holder)`; holder is mandatory. Reader lookup becomes `latest(provider, credential_refs)`, where each reference is `(is_global, profile, credential_id)`. Scope both producer and consumer changes together. Unscoped v1 files are not trustworthy for cross-profile attribution: **refuse them**, with opaque/no-capture behavior, no automatic attribution/migration. Do not write synthetic flags through the production observer. Preserve refusal of file/entry `synthetic:true`; refuse unexpected schema versions.

Maintain 256 KiB/256-entry bounds from `captures.py`; validate a bounded existing store before reuse. Serialize cross-process read-modify-write with a same-directory lock, with a maximum 100 ms lock wait then drop the observation; atomic replacement alone is insufficient. Use a standard-library platform-supported lock; if unavailable, skip writes rather than pretending thread-only locking protects multiple gateways. Hold lock through read/validate/replace, retain the newest timestamp per key, never regress on out-of-order delivery; at capacity retain the newest observations (deterministic tuple tie-break), not alphabetically first credentials. Snapshot reader never observes partial JSON. Reject timestamps in the future relative to reader clock (no fabricated observations).

Create private directory/files (0700/0600 on POSIX), reject symlink traversal for store ancestors, store, lock and temporary paths, and confine them below the host-resolved root. Do not follow a symlinked `profiles` parent during machine enumeration either. Filesystem failure, malformed data, contention, unavailable config/hook, missing identity and unsupported host version all fail closed for telemetry and fail open for normal inference: no extra requests, no raw exception/header/path logging. Sink errors are swallowed with fixed path-free diagnostics; no fake zero, no overwrite of a good entry by an older response. On a malformed store the writer skips that write rather than replacing unknown content. Preserve normal card reason/error semantics from §9.1.

The setting authorizes this isolated plugin-data write only; auth.json, .env, pool health, config and analytics stay read-only. No dashboard API calls `record_headers`.

### 12.4 Codex decision — no live probe in this change

> **Historical; superseded for one exact route.** This decision was correct when ratified. The later Codex amendment (architect `t_ad8bf9bc`, §9.2 and `docs/decisions/codex-wham-usage.md`, backed by a recorded HTTP 200 and sanitized body) overrides the no-probe / no-allowlist-exception / keep-opaque rules below **only** for GET `https://chatgpt.com/backend-api/wham/usage` with the selected Codex access token and its same-token routing claims. Everything else below stays in force: no other Codex origin or path, no refresh, no credential mutation, and the §12.5 scope rule (only the SELECTED profile's effective credential may be sent; machine-only holders issue no request). The URL-provenance correction below stands.

**Do not probe Codex live. No origin/path allowlist exception is ratified.** Keep quota opaque, no new Codex network request or inference call. Reading source is not evidence that httpx succeeds or that response fields describe subscription windows. The recon artifact's success statement is explicitly not accepted as live evidence.

Correct the provenance text: at inspected Hermes rev b200f5bc, `agent/account_usage.py:529–535` retains `/backend-api` in the normalized base. Its default maps to `https://chatgpt.com/backend-api/wham/usage`, NOT the recon's `https://chatgpt.com/wham/usage`. Neither URL is thereby approved or proven reachable. Update DESIGN §9.2/README/tooltips wherever they repeat the incorrect URL; prefer 'source-derived candidate endpoint; not live-verified' in UI.

Future reopening requires a separate explicit authorization identifying exact GET origin/path, accounts, allowed identity headers and privacy scope; one bounded read-only attempt per approved account, TLS verification, redirects disabled, no refresh/consume/reset actions and no anti-bot bypass. Record HTTP status, time, exact nonsecret target, and allowlisted numeric/enum body or redacted shape; never full bodies containing account details. A failed/blocked probe remains opaque. Ratifying a runtime adapter requires a further schema/window/scope mapping decision after evidence, not automatic promotion upon HTTP 200. This card creates no probe task and grants no consent on the user's behalf.

### 12.5 Ratification of §§9–11, with bounded amendments

**Ratified:** Anthropic session_5h/weekly_7d mappings, utilization ×100 with no data clamping, independently reported resets_at, receipt-time observed_at, no extrapolation, reset-past windows dropped, per-meter Exact only for usable captured provider numbers. Exact means exact last observation, not real-time current usage. Captures with future observations or ambiguous holder/credential provenance do not qualify. Missing capture is opaque; no source still means no meter, not zero.

**Ratified:** `holders`, `resets_at`, `anthropic_headers` source and `/subscriptions data.view` are additive wire-v1 fields with required serialization/nulls as in §§9–10. No raw JWT/provider identity or capture reference may appear in public DTOs/debug. The local capture schema bump does not bump wire schema. Correct the empty-success example in §3 to include `view`.

**Ratified:** machine default inventory over root plus at most 64 direct valid profiles, with `view=profile` retaining existing effective profile/global shadowing; chart remains selected-profile only. Explicit exception to §1.1 permits bounded auth.json inventory reads from other homes, not their config/.env/analytics, credential refresh, or new provider endpoints. This is a same-OS-owner dashboard inventory view, **not tenant isolation**: deploy only under the existing full-trust machine-owner dashboard gate. If a host offers profile-restricted dashboard principals, force profile view for those principals; a query string cannot widen authorization. Do not add another login/credential UX.

**Amended identity policy:** JWT grouping is a private display-dedup heuristic, never authenticated identity or proof of disjoint billing allowances. Use it only for canonical `openai-codex`, only when BOTH valid `chatgpt_account_id` and valid user claim (`chatgpt_user_id`, then `user_id`, then `chatgpt_account_user_id`) exist under the exact `https://api.openai.com/auth` claim. Missing either means no JWT merge. Account-only workspace identity cannot collapse different seats. Unverified JWT fields may not authorize network targets or pool mutations. This narrowly supersedes §1.1's ban on using JWT claims for display dedup identity, not its ban on inferring entitlements from plan claims. No subject/email/token fallback.

**Amended fallback:** do not merge accounts across different holders solely because public pool row IDs match; they are store-local identifiers, not proof of a shared provider account. Within each store preserve exact-row dedup. For machine identity without a complete allowed JWT pair, use holder + provider + normalized endpoint + public row id/ordinal. Include endpoint in all merge grouping; self-hosted health remains one row per endpoint even when account cards stay separate. This may show duplicate opaque accounts across homes; false separation is safer than misattributed usage. For JWT-merged cards use the first credential/plan deterministically (root first, then sorted profiles), union secrets only for redaction and consult all scoped capture references; do not sum balances or infer shared windows.

For stable machine card IDs, replace §10's loosely specified identity string with `identity = json.dumps(parts, separators=(",", ":"), ensure_ascii=True)`. Complete JWT parts are `["jwt", provider_id, normalized_endpoint, account_claim, user_claim]`; fallback parts are `["row", holder.is_global, holder.profile, provider_id, normalized_endpoint, public_row_id_or_ordinal]` (public id string or one-based integer ordinal). Then retain §10's existing `make_account_id(provider_id, normalized_endpoint, "machine:" + identity)` hash rule. Structured JSON prevents delimiter collisions in claims. Selected-source precedence is root, then profiles alphabetically, then each store's original row order. Scope references are private typed tuples, never DTO keys.

Machine-wide auth enumeration is inventory permission, not permission to **use another profile's credentials** for outbound probes. Preserve approved OpenRouter/local health probing, and the one Codex route reopened by §9.2 (which postdates §12.4), for the selected profile's effective credentials/config only; for a merged Codex card the bearer AND the routing claims sent with it come from that same selected member's token, never from another member. For machine-only holders render local inventory and eligible existing local captures; no outbound requests with their credentials. A merged card may use a selected-profile effective credential if one is among its members, not silently use whichever member was first. Tooltip must explain when machine-only inventory has no eligible live source. Profile-isolated caches must also include effective view and root/source identity; no sibling credentials in logs or DTOs.

**Ratified with wording update:** §11's badge policy and presence-only holder/plan tooltips. 'Hermes does not record these today' must be version/availability-qualified once the observer ships: 'No usable capture; enable the optional observer and use this account normally.' Never promise an observation before an ordinary request returns eligible headers.

### 12.6 Implementation/verification contracts

Two independently reviewable changes, no production writes:

1. Hermes core hook: request-local provenance and sanitized interface exactly §12.2, all normal request behavior unchanged. Tests: stream/nonstream once, duplicate prevention, rotation/concurrency/global fallback, missing owner/id, proxy/API-key/Bedrock exclusion, no subscriber and subscriber raising, invalid/secret-bearing headers filtered before dispatch. Register/document hook. No UsageDeck import or persistence. Ship PR/commit handle and test output.
2. UsageDeck observer + alignment: §12.1 opt-in, §12.3 schema2 reader/writer/ref propagation, §12.5 identity/scope amendments, §12.4 source-text correction, README/DESIGN incorporation and deterministic dashboard staging. Test opt-out produces zero writes and network calls; eligible synthetic *test-only* callback reaches the real schema2 seam; two holders with same row id never share captures; global fallback attribution; account-only JWT/no-user does not merge; complete JWT same seat merges but separate users do not; endpoint separation; machine-only credentials never probe; symlink parent/store/lock rejection; malformed/oversize/future/synthetic/out-of-order data; actual multiprocess concurrent writers and lock timeout. Run full backend/frontend/packaging/actual-host harness with scratch credentials and **no new live inference/probe**. Fixture outputs are not live observations.

Each implementer submits for independent review; the architect who authored this design must not review its own design implementation. Do not merge/install merely because this decision card is done. Original PR6 is not blanket-approved: its unscoped v1 capture, incomplete-user JWT merge, cross-holder row-id grouping and machine-only credential probing need these amendments before representing compliance with the ratified design.

### 12.7 Implementation notes (card `t_3e2434b4`)

Non-normative record of how this repository realises §12; the normative text is §12.1–§12.6 above.

- **Observer** (`observer.py`): a plain-function subscriber `observe(**payload)` registered by the package-level `register(ctx)` (the Hermes agent-plugin convention) only when the host's `VALID_HOOKS` knows `provider_ratelimit_headers`; unknown/older hosts skip. The host adds envelope metadata of its own (`telemetry_schema_version`) to every hook call, so extra payload fields are ignored unread while every §12.2 field is required and `schema_version` must be the integer `1`. The observer re-validates the payload against the host context it runs in (root and execution profile must be exactly the running home's; the holder must be that profile or the root), reads `usage_deck.capture_anthropic_headers` (only boolean `true` opts in) through `hermes_cli.config.load_config_readonly()` for that home, sanitizes headers again and writes through `captures.record_headers`. It imports nothing from the dashboard service and makes no request.
- **Capture store** (`captures.py`): schema 2 as §12.3. Reads and writes use `openat`-style descriptors with `O_NOFOLLOW` at every path component below the host root (a symlink anywhere refuses the operation); the writer holds an `flock` on `.anthropic-ratelimit.lock` (same directory, max 100 ms wait, then the observation is dropped), validates the existing store strictly (any malformed/oversize/v1/synthetic content skips the write and leaves the file untouched), keeps the newest observation per scoped key, evicts oldest-first by `(observed_at, key tuple)` within 256 entries / 256 KiB, writes a 0600 temporary file and renames it atomically. Platforms without POSIX `dir_fd`/`flock` skip writes. A v1 store (never produced by any shipped writer) must be deleted manually to start capturing.
- **Scoped references**: `InventoryItem.credential_refs` carries private `(is_global, profile, row id)` tuples taken from the owning store (global fallback rows are the GLOBAL store's; machine rows are their source store's). `CaptureStore.latest(provider, refs, now=)` never looks up a bare row id and refuses observations later than the refresh clock (+1 s).
- **Machine inventory** (`discover_machine`): `identity_json` parts exactly as §12.5; a member supplies the card's credential only when it belongs to the SELECTED profile's effective pool/legacy/config (`InventoryItem.live_source`); `AdapterContext.live_source=False` makes OpenRouter, Codex (§9.2) and self-hosted adapters issue **no** request, and the service grants such an item no outbound target at all (card: opaque, `source_unavailable`; health row: `unknown`). For a merged Codex card the credential and the account/residency routing headers are derived from the one selected member's token (the masking context still unions every member's private routing values). Env-derived credentials attach to selected-profile items only. The service cache key includes root, home, profile and effective view.
- **Profile-restricted principals**: the router passes `profile_only=True` when the host stamped `request.state.native_http_principal` (a grant bound to one profile); the service then clamps `view=machine` to `profile` while still validating the query.
- **Verification**: see README "Tests"; the real core hook is exercised by `tests/test_core_seam.py` when `USAGE_DECK_CORE_CHECKOUT` points at a Hermes checkout carrying the hook (core card `t_3a6bf27f`). All capture data in tests and the host harness is synthetic and is not a live observation.

### 12.8 Evidence inspected (local, pinned)

UsageDeck source at 30bc686811bfb634923f6743e5ec1ad56dc3602b:
- DESIGN.md:19–31,82–88,287–352 — original invariants and pending deltas.
- plugins/usage_deck/captures.py:40–54,121–160,166–205 — v1 unscoped keys, bounded reader, atomic but unlocked writer.
- plugins/usage_deck/discovery.py:177–216,338–364,500–510,551–628 — profile enumeration, missing-user JWT fallback, row-id merge and members.
- plugins/usage_deck/service.py:128–138 and adapter orchestration — global capture reader/view selection; no opt-in producer.
Hermes source at b200f5bc60e31588ba2cb60e288029c4a54ba5fa:
- agent/rate_limit_credits.py:74–78 — existing raw Anthropic callback parses rate-limit/credits state only.
- agent/client_lifecycle.py:950–959,987–998 — mutable selected row field and stream/native on_response seam.
- agent/turn_response_intake.py:66–108 — post_api_request has no headers.
- agent/account_usage.py:529–535 — source-derived URL correction (no live success proof).
Recon attachment t_7dce2222/recon_report.md:61–67,97–114 — source-only derivation, unsupported live-success assertion; not a probe transcript.
Official docs retrieved: Hermes docs llms.txt, user-guide/features/plugins and user-guide/features/hooks. They establish plugin registration and hook isolation patterns, not existence of the newly designed event. Local pinned source is the implementation baseline. No provider documentation or live quota response newly verified by this card.
