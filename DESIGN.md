# UsageDeck MVP contract — v1

Status: **ratified; backend and frontend implementation released**.
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

Read credential pools profile-first, with global fallback **per provider only when the profile has zero pool entries for that provider**. A nonempty profile pool replaces the global provider pool; do not combine their accounts. This matches `hermes_cli/auth.py:934–958`. Legacy `providers` entries are used only when no effective pool exists for that provider. Add configured primary/custom providers even without credentials. Use the host's configuration loader (including its YAML support), never a hand-written YAML parser. Do not scan other profiles or read `.env` wholesale. Resolve only the credential references required by a discovered adapter, through host helpers. Never refresh OAuth, rotate credentials, persist pool health, or invoke an inference request.

One SubscriptionCard per effective pool account; one per configured endpoint without a pool account. A configured endpoint and pool row referring to the same provider/base URL are one inventory item, not two. Dedupe exact duplicate stored row IDs and normalized endpoints, not different accounts just because their provider matches. Do not dedupe different stored row IDs by hashing their secret values.

Preserve stored `provider_id` literally (including case/punctuation for custom names); registry aliases are lookup-only and must not rewrite response IDs. Canonical built-in IDs: `nous`, `openrouter`, `openai-codex`, `anthropic`, `ollama`. A configured custom name with no stored ID gets `custom:<configured-name>` exactly as configured. Match configured names case-insensitively only if the match is unique. Never infer provider identity from a model-name prefix.

`account_id` = `acct_` plus first 24 lowercase hexadecimal characters of SHA-256 over UTF-8 JSON of `[provider_id, normalized_endpoint, public_row_id_or_ordinal]` (compact separators, ensure_ascii=True). Public row ID is a stored non-secret row `id`; otherwise a one-based source-order ordinal. Endpoint normalization removes trailing `/`, userinfo, query and fragment; lowercase scheme and hostname, preserve path/port. IDs may change if an anonymous account's source order changes; never derive them from tokens, emails or JWT subjects. `endpoint_id` uses the same algorithm with `ep_` and `[provider_id, normalized_endpoint]`. Card `id` equals `account_id`. No secrets appear in ID inputs.

Display account labels as `Account 1`, `Account 2`, etc. in source order, not emails or provider account IDs. Plan metadata may be extracted from known plan-name claims in existing JWTs locally, without verifying their authority; allowlist short strings and set `plan_source=unverified_claim`. Never send JWTs anywhere, and never use a plan claim to infer a quota, billing cycle, account identity or entitlement. Unsupported claims yield null plan. No live fleet addresses or credential assumptions in code.

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
| OpenAICodexAdapter / openai-codex | local inventory, safe plan claim, AnalyticsReader | Subscription quota opaque. Never send Codex OAuth to OpenAI billing endpoints; API org billing is not a ChatGPT subscription quota. |
| AnthropicAdapter / anthropic | local inventory, safe plan claim, AnalyticsReader | OAuth/ordinary inference credentials do not establish org analytics or subscription quota access. Quota opaque. No SDK or admin endpoint guessing. |
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

Only GET. Fixed cloud origin/path allowlist. Local requests must match inventory-configured http(s) origin; endpoints are never accepted from query strings. Derive `/metrics` and health paths from origin, not by blindly appending to `/v1`. Reject URL userinfo and credential-bearing query/fragment for transport; do not log the rejected raw URL. No redirects, including same-origin; no credentials copied to another origin. Verify TLS. Do not send inference credentials to `/metrics` by default; use only a separately configured metrics credential if one already exists. No new connection/credential UX in MVP.

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
  unit: usd | count | percent | bytes,
  scope: api_credential | provider_account | endpoint | profile,
  period: Period,
  confidence: ConfidenceLabel,
  source: openrouter_key | session_analytics | prometheus | none,
  observed_at: timestamp|null
}

SubscriptionCard {
  id: string, provider_id: string, account_id: string,
  provider_label: string, account_label: string,
  kind: cloud | self_hosted,
  plan_name: string|null,
  plan_source: unverified_claim | provider | none,
  auth_status: available | missing,  # presence only, NOT validity
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

### 2.1 Secret masking at DTO construction

Use one shared safe factory before any DTO exists, caches, logs, drawer data or response serialization. Allowlist fields above; no raw payload bag. Recursively redact values under any field name containing (case-insensitive) `key`, `token`, `secret`, `password`, plus authorization, cookie, credentials, bearer, jwt, access, refresh. Replacement is literal `[REDACTED]`; recurse through lists/maps and reject non-JSON objects. Do not retain suffixes or fingerprints. This intentionally forbids wire names like `input_tokens` and `api_key_id`; map allowed numeric analytics fields into `input_count`/`output_count` **before** the DTO factory. Do not allow a token-count exception that can also leak credential fields.

Additionally scrub all displayed strings and nested values for the exact nonempty secret values in the current private context (including values under generic names), credential-looking prefixes/JWT/Bearer patterns, and URL userinfo/query/fragment. Reject a configured display string that contains such a value; use `Provider`/`Account N` instead. Reason/error text comes only from fixed local tables. Arbitrary upstream strings never become labels. Construction must not retain raw private context or reference mutable raw dicts. Debug drawer contains only `SubscriptionCard`'s already-sanitized JSON, not a second debug endpoint or network response. Frontend uses text rendering, never innerHTML. Unit-test redaction under generic names, mixed-case keys, nested arrays and exception text.

## 3. API Routes and envelopes

Required physical endpoints (approved in §0):

- GET `/api/plugins/usage-deck/subscriptions`
- GET `/api/plugins/usage-deck/usage?window=7|30|90&provider=<id>`
- GET `/api/plugins/usage-deck/health`

Export a relative router (`/subscriptions`, `/usage`, `/health`) with **no prefix**; the host supplies `/api/plugins/usage-deck`. A test harness mounts it at the same API_BASE; actual host namespace must be verified in integration QA, not merely a standalone TestClient. The old `/api/usage-deck/*` namespace is not exposed.

Common optional query `profile` is passed through the host profile validation/scope mechanism. Omitted = dashboard active profile. All endpoints inherit the dashboard auth/consent/profile gate; no public bypass and no provider credentials in requests. Unknown query parameters and repeated single-value parameters => 400 invalid_query. Window defaults to 30; accept only the literal strings `7`, `30`, `90` (not 07, floats or arbitrary days). Provider absent means unfiltered; empty string or >256 characters/control characters => 400; percent-decode once through the host, match exact discovered provider ID. Unknown inventory provider => 404 provider_not_found (this does **not** mean a discovered provider with unknown adapter should fail).

Every endpoint's plugin-owned success response (HTTP 200):

```json
{"ok":true,"data":{},"error":null,"meta":{"schema_version":1,"request_id":"uuid","generated_at":"2026-11-06T00:00:00Z","partial":false,"errors":[]}}
```

`data` by suffix:

```text
/subscriptions: { cards: list[SubscriptionCard], inventory_state: empty|detected }
/usage: { window_days: 7|30|90, provider_id: string|null,
          series: list[UsageTimeSeries], reason: Reason }
/health: { entries: list[HealthStripEntry] }
```

Failure response (plugin-owned 400/404/503/500): `ok=false`, `data=null`, `error=DeckError`, same `meta` shape. 400 malformed query, 404 provider not discovered, 503 unreadable/malformed inventory or unavailable required analytics, 500 invariant failure. Successful inventories with zero entries are 200 empty, not 404. Missing inventory files mean empty discovery, not unreadable inventory; report malformed JSON/config without leaking paths or contents. Host 401/403 gates may return their native error shape before plugin code runs; frontend treats HTTP status as authoritative and never assumes plugin envelope on host failures.

Per-provider partial failures are HTTP 200, `meta.partial=true`, `meta.errors` populated and matching card/health errors; do not 500 a whole grid for an upstream 401. Unsupported capability/unknown attribution is a nonerror opaque result and does not alone make partial=true. Whole-profile unavailable analytics is 503 for `/usage` only; subscription inventory and health remain usable. No credentials required means no credential wall, not that the dashboard authentication is optional.

Return all inventory cards/health rows without pagination; sort cards by `(provider_id, account_id)`, health by `(provider_id, endpoint_id)`, series by id and points by day ascending. No cursor/count/has_more fields. No cross-account provider-total meter unless source proves disjoint identity and periods; MVP does not. Provider filtering may match several accounts but does not sum duplicated account-wide balances. No account filter in v1.

Example empty success: `data={"cards":[],"inventory_state":"empty"}`. Example unknown adapter card: `state=opaque`, `confidence=opaque`, `reason=unsupported`, `meters=[]`, `error=null`; the profile chart is separately estimated. Source-specific units or missing limits are never filled from UI guesses.

## 4. UI State Machine

Three independent zones, one screen: subscription grid, usage chart (7/30/90 picker + provider picker), self-hosted health strip. A failure in one zone never erases successful siblings. Use only host SDK's React/components/hooks/fetchJSON; inherit selected profile and auth behavior. Plugin tab `/usage-deck`, label UsageDeck, position `after:models`; unsupported positioning may fall back to host's default insertion order. Accessible status text, keyboard drawer controls, focus restoration and aria-expanded; status is not color alone.

Global `first_run`: entered on initial mount/profile change before successful inventory retrieval. Immediately fetch inventory and show loading skeleton, no login/provider-key form. Successful empty inventory stays first_run and shows “No providers detected in this profile” with Refresh and host configuration guidance, without opening a credential input. Successful nonempty inventory transitions to `connected` regardless of telemetry capability or credentials. Connected → first_run when later inventory is empty or profile changes; network errors do not fabricate an empty inventory. Host 401/403 shows dashboard access error, not provider credential collection. Session-only chart may render even in first_run.

Per-card `loading` is frontend-only; server returns terminal CardState. Loading → error for retryable/transport/malformed/auth-source errors; → opaque for unsupported/missing-permission telemetry or no quota capability; → empty only for supported telemetry explicitly reporting no observations; → ready when at least one supported non-null measured value exists. Explicit observed zero is ready, never empty. Healthy self-hosted cards with no billable meter remain opaque while health is independently useful. Unsupported missing quota alongside an available meter does not suppress that meter; source badges remain metric-specific.

Ready/error/opaque/empty → loading on refresh/profile change. During a same-profile refresh keep prior sanitized values with stale indicator and aria-busy; a failed refresh transitions error, keeps prior data visibly stale, and removes its progress meter. Never retain prior-profile data. Error → loading only via polling/Refresh; late old-window/profile results are discarded. Opaque has “Usage unavailable”/“Session analytics only” copy, not a grey zero-percent meter. Empty has “No observations for this period.” Loading skeleton has no numeric value/badge pretending to be observed data. Expand drawer in any loaded terminal state; display only sanitized DTO.

Chart: window defaults 30; preserve selection within mount, reset profile data before refetch. Unfiltered means profile-wide session analytics; subtitle explicitly “This Hermes profile only — not subscription allowance.” Show input/output counts and estimated USD as separate charts or distinct axes with units; never sum counts and dollars. A filtered unavailable series says “Provider attribution unavailable from session analytics.” Do not plot fake zero lines, extrapolate resets, or join gaps. Error and empty chart states are separate from inventory states. No chart dependency: use React + native SVG. Health strip uses text/status icon, latency, and measured CPU/memory only when non-null; no quota bar from CPU percentages.

## 5. Confidence Rules

Wire `self_hosted` is canonical, display badge is **Self-hosted** (resolves brief's hyphenated prose vs DTO enum). Other badge text: exact → **Exact**, estimated → **Estimated**, opaque → **Opaque**. Tooltip must name source, scope and observed time. Every number/meter/chart and health entry carries its own source badge; never rely solely on a card header badge.

- exact: only directly reported numeric quantity for a known scope/period. OpenRouter key balance/usage can qualify; it is not an exact subscription percentage.
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
 plugins/usage_deck/discovery.py         backend
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

## Grounding references

Local Hermes checkout inspected for this design:
- `plugins/kanban/dashboard/manifest.json:1–14` and `dist/index.js:8–24,674–707`: real manifest and host SDK usage.
- `hermes_cli/web_server_dashboard.py:1019–1043,1239–1247`: isolated forwarding and mandatory plugin route prefix.
- `hermes_cli/plugins_cmd_inspect.py:57–87`: installation payload detection.
- `hermes_cli/auth.py:934–958`: per-provider profile-first pool resolution.
- `hermes_cli/web_routers/analytics.py:84–159`: actual analytics output/rolling days/local dates and lack of provider-scoped daily rows.
- `docs/recon-providers.md`: upstream observations; not proof of entitlement or automatic admin API access.

Official Hermes documentation index was retrieved and identifies “Extending the Dashboard” as the authoritative topic. Its page and the OpenRouter current-key reference were downloaded, but this run could not reliably extract their body text (extract backend unavailable; browser lacks libglib). Accordingly, the decisions above are grounded in the inspected local source and the upstream recon, not claimed as newly verified provider documentation. No new Anthropic analytics endpoint or SDK assertion is adopted. Local Hermes source revision inspected: `b200f5bc60e31588ba2cb60e288029c4a54ba5fa`.
