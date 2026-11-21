# API-equivalent pricing

Status: ratified design for t_17620c2f; 2026-11-21. Documentation only.
Base: `c4a662d2e9b00644d9ddd4a5bc332f381a3a2680`.

## 1. Scope and ownership

API-equivalent is a hypothetical **current base text API tariff** applied to the recorded input/output tokens, not an invoice, historical tariff, subscription cost, or claim of actual expenditure. No discounts for quantization. Do not add cached/reasoning tokens to counters, infer missing tokens, or apply context tiers, batch discounts, tool fees, media rates, or cache discounts. Explain these limitations in the UI.

Preserve session IDs, model IDs, provider attribution, input/output counts, existing `estimated_usd`, `estimated_cost_usd`, `cost_source`, spend formatting and subscription meters. Pricing is a separate read-time annotation. Never write to any `state.db`; retain `mode=ro`, authorized-home validation, provider filters, window boundaries and cross-home isolation.

Canonical new files (implementation, not part of this docs commit):

- `plugins/usage_deck/pricing.py`: pure resolution plus catalog/cache manager.
- `plugins/usage_deck/pricing_data/aliases.json`: curated identities and proxies.
- `plugins/usage_deck/pricing_data/seed-v1.json`: validated normalized offline catalog.

Resolver worker owns those three files, catalog tests and the staging-list additions. UI/integration worker owns additive DTOs, service/dimensions integration and frontend. Both must use the signatures and JSON shapes below; frontend does **not** fetch public prices or implement resolution in JavaScript. Integration worker consumes the resolver module or a test double of this contract while working in parallel. Packaging worker verifies final mirrors after integration; generated files are never hand-edited.

## 2. Sources and precedence

**Primary: LiteLLM. Secondary: OpenRouter.** Endpoints, fixed in code (never derived from session IDs):

- `https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json`
- `https://openrouter.ai/api/v1/models`

Direct anonymous fetches from this workspace on 2026-11-21 at 00:57 UTC returned parseable catalogs: LiteLLM 7,078 top-level entries (not all usable text prices), OpenRouter 477 `data` entries. These are observations, not constants to assert in tests. Payload SHA-256:

| Source | SHA-256 |
| --- | --- |
| LiteLLM | `c436a6e5121c438a20b0c014d46129acfe41d2d1b23725bfda733dd901141210` |
| OpenRouter | `de55929313b48b7a2c189acc939f097efa6dc790fa353159672bce31768d8fd1` |

Evidence is the actual JSON at the endpoints above, inspected for the keys in §4; parent brief t_1c0734e0 contains the earlier 2026-11-20 recon. LiteLLM has substantially broader provider/model coverage, including all chosen alias targets. OpenRouter has convenient vendor/model IDs but narrower coverage and may describe router rather than a particular provider's tariff. Rates can differ materially: the inspected `aiand/qwen/qwen3.8-27b` is USD 0.40/3.00 per million input/output tokens, while OpenRouter `qwen/qwen3.8-27b` is 0.06/1.96. Do not average, select the cheapest, or silently equate providers.

### Normalized identity and overlap

Catalog keys are source-qualified, case-preserving strings: `litellm:<exact upstream key>` and `openrouter:<exact upstream id>`. Parse only the first colon as the source separator. Never conflate same-suffix regional/provider variants. LiteLLM rates come from `input_cost_per_token` and `output_cost_per_token`; OpenRouter rates from `pricing.prompt` and `pricing.completion`. Both are USD **per token**, not per million.

Accept only finite nonnegative decimal values for **both** components, never booleans, null, negative sentinel values, NaN or infinity. Zero is valid. LiteLLM entries must have `mode == "chat"` or `mode == "completion"`; OpenRouter entries must have text among `architecture.input_modalities` and `architecture.output_modalities`. Skip metadata/non-text/per-request-only records; do not infer missing rates. Retain original numeric precision by parsing JSON numbers as Decimal and serializing rates as decimal strings. Skip invalid entries with a source-level count in logs; an empty usable catalog is a source failure.

Keep both sources' records. Resolution precedence is: explicit alias target, then heuristic candidates from LiteLLM, then OpenRouter only when LiteLLM has **no** eligible match. A primary ambiguous match returns unknown, not a secondary guess. An alias pins a provider and may explicitly choose OpenRouter; it overrides source preference. For an identical semantic signature across sources, primary wins even on a rate mismatch; record both rates in diagnostic logs when comparing, never replace a primary price due to difference alone. Heuristic-equivalent provider variants inside one source remain ambiguous (§5).

A failed primary request never authorizes repricing an existing primary key with a secondary key. Retain cached primary records and supplement with secondary-only matches. There is no undocumented source-key rewrite. Seed records are the final per-key fallback, including when an upstream entry is later delisted.

## 3. Cache, refresh and offline behavior

UsageDeck data dir is `<authorized root>/plugin-data/usage-deck` (consistent with `captures.py`). Cache: **`pricing/catalog-v1.json` relative to that data dir**. This is the plugin's writable data, not a session store or viewed profile's DB. Bind the manager to the inventory's validated root; do not use request-supplied arbitrary paths, fall back to another root, or share request usage data across roots. Read-only/unwritable plugin data is supported: use in-memory snapshots and seed without disk persistence.

- Load and validate packaged seed synchronously; overlay valid disk entries; render immediately without waiting for network.
- Schedule background refresh on first usage request/startup when due. Single-flight per manager/root; freeze one immutable catalog per analytics response.
- Successful source fetch: `next_refresh_at = fetched_at + 24h * U(0.9, 1.1)` (21.6–26.4 hours); draw once and persist per source. Inject clock/random for tests. On restart honor due time; clamp corrupt/far-future times to due now.
- Fetch both sources independently each refresh cycle, primary first, to fill gaps. HTTPS only, no credentials, at most 16 MiB per response, 5s timeout per attempt, at most 2 attempts per source with 0.5–1.5s retry jitter; background work only. Restrict redirects to the endpoint's original host; no network access based on model/catalog content.
- Failed refresh: preserve entries, `fetched_at`, and current response; set a sanitized source error and retry after `15min * U(0.9, 1.1)`. No hot-loop per render. HTTP/schema/parse/timeout/empty response all count as failure. Log source, fetched_at, attempt time, result/count; no tokens, credentials or usage rows.
- Overlay successful records **per key**, preserving absent old keys and their original per-entry `fetched_at` (delisted prices must not be made fresh). A failed source cannot erase another source or last-known data. A key may change rate only on successful validation of that key.
- **Stale warning at age > 7 days** per selected entry, including seed age. No hard expiry: retain old prices indefinitely offline, visibly stale, never silently blank values. Future fetched_at beyond 5min is invalid. Source failure may warn before staleness; failure never resets age. Group/total stale if any priced contributing entry is stale.
- Validate complete disk envelope before promotion. Corrupt/unknown-version cache -> seed + warning; do not delete it before a validated replacement is available. Atomic temp-file + replace within same directory, mode 0600, parent 0700; reject symlink cache paths/parents and non-regular files. Lock/serialize writers; disk write failure leaves in-memory and existing disk intact.

### Seed and disk schema (same schema version)

```json
{
  "schema_version": 1,
  "sources": {
    "litellm": {
      "fetched_at": "2026-11-21T00:57:39.831243Z",
      "next_refresh_at": "2026-11-22T00:57:39.831243Z",
      "payload_sha256": "c436a6e5121c438a20b0c014d46129acfe41d2d1b23725bfda733dd901141210"
    }
  },
  "entries": {
    "litellm:azure_ai/gpt-6-luna": {
      "source": "litellm",
      "source_key": "azure_ai/gpt-6-luna",
      "input_usd_per_token": "0.0000001",
      "output_usd_per_token": "0.0000005",
      "fetched_at": "2026-11-21T00:57:39.831243Z"
    }
  }
}
```

Example is deliberately abbreviated, not a fabricated full seed. `sources` may contain both source IDs; source objects have exactly these three fields. Diagnostics/retry times live in manager state, not in rate records. Derive entry identity/signature from `source_key`; all entries use the five fields above. Validate source/key prefix agreement. Catalog `revision` is SHA-256 of canonical normalized entries (sorted keys, compact UTF-8 JSON); it changes on rates/entry timestamps and is internal, not an upstream version.

Ship a real fetched/normalized snapshot, not hand-invented prices: all usable entries from both sources, **at minimum all seven alias target records**. Include retrieval timestamps/digests and source attribution. Build must fail if any seeded alias target is missing or invalid. Seed age is original retrieval time, never install/build time; seed refresh requires deliberate repository update. Runtime cache can overlay it, never overwrite packaged assets. JSON avoids adding a YAML parser dependency.

## 4. Curated aliases and seven-model inventory

`aliases.json` is a versioned UTF-8 JSON object; exact recorded session ID is the map key (case-sensitive, no stripping before this lookup). Reject duplicate keys. `catalog_key` is one source-qualified key, `kind` is `equivalent` or `proxy`, and nonempty `note` is required and displayed. No executable patterns or user path/URL values. Normative seed table:

```json
{
  "schema_version": 1,
  "aliases": {
    "gpt-6-luna": {"catalog_key": "litellm:azure_ai/gpt-6-luna", "kind": "equivalent", "note": "Prices as the cataloged Azure AI GPT-6 Luna text tariff; not subscription spend."},
    "gpt-6.1-sol": {"catalog_key": "litellm:azure_ai/gpt-6.1-sol", "kind": "equivalent", "note": "Prices as the cataloged Azure AI GPT-6.1 Sol text tariff; not subscription spend."},
    "claude-sonnet-5-5": {"catalog_key": "litellm:claude-sonnet-5-5", "kind": "equivalent", "note": "Exact LiteLLM model key; original session ID remains unchanged."},
    "qwen3.8-flash-next": {"catalog_key": "litellm:dashscope/qwen3.8-flash", "kind": "proxy", "note": "Prices as Qwen3.8 Flash; custom -next identity is not independently verified."},
    "qwen3.6:35b": {"catalog_key": "litellm:aihubmix/qwen3.6-35b-a3b", "kind": "proxy", "note": "Prices as Qwen3.6 35B A3B; recorded 35b does not prove A3B variant."},
    "cyber-tiel-coder:35b": {"catalog_key": "litellm:aihubmix/qwen3.6-35b-a3b", "kind": "proxy", "note": "Design-selected 35B coding price proxy, Qwen3.6 35B A3B. Cyber-tiel brand has no catalog identity; this is not a verified underlying model."},
    "qwen3.8-hauhau-aggressive-27b-iq3-gpu-96k-b8k": {"catalog_key": "litellm:aiand/qwen/qwen3.8-27b", "kind": "proxy", "note": "Prices as full Qwen3.8 27B regardless of iq3/GPU settings; custom hauhau-aggressive variant identity is unverified."}
  }
}
```

**All seven use `method=alias`**, deliberately satisfying the implementation card's requirement to seed all seven, not pretending ambiguous custom names are heuristic facts. All six distinct target keys and their two numeric rates were verified in the live LiteLLM payload above. The seven rows' per-million input/output tariffs observed in that payload are respectively: 0.10/0.50; 2.00/10.00; 2.00/10.00; 0.15/0.47; 0.254/1.524; 0.254/1.524; 0.40/3.00. Implementation must derive these from the seed, not hardcode arithmetic separately.

The cyber-tiel choice is an explicit **price proxy design decision**, not evidence that this fictional brand is Qwen. This caveat is mandatory wherever explaining its value. Proxies participate in API-equivalent sums, with proxy counts/labels visible. Do not call these actual costs or exact identities. If an alias target disappears, look up retained cache then seed for that **same key**. If still unavailable, return unknown `alias_target_missing`; do not fall through to a different heuristic. Invalid whole alias file -> safe empty map plus catalog warning; shipping invalid aliases is a packaging/test failure.

## 5. Exact heuristic fallback

Order: **exact curated alias > heuristics > unknown**. Heuristics are local catalog search, never a network query. Preserve original ID for grouping/attribution and UI. On the pricing-only copy:

1. Validate with existing safe model-ID/redaction rules; absent/unsafe ID -> `model_unknown` without echoing unsafe text.
2. Try exact case-sensitive upstream `source_key` match (primary first), provided it has usable rates. This is still `method=heuristic`; no fourth method named exact.
3. Lowercase for comparison. Replace underscores with hyphens. Extract terminal `:(\d+(?:\.\d+)?)b` as **parameter-size token**, then replace with `-<size>b`; do not discard size. No generic colon stripping: `:free`, `:batch`, `:thinking` remain meaningful and cannot map to paid/base automatically.
4. Remove terminal quant/hardware decorations **repeatedly until stable**, using boundary-anchored suffix patterns `-iq[2-8]$`, `-q[2-8](?:-(?:k|s|m|l))*$`, `-gpu-[0-9]+k$`, `-b[0-9]+k$`. After step 3's underscore replacement, `-q4_k_m` is `-q4-k-m` and is removed as a whole. The only accepted quant modifier tokens are `k`, `s`, `m`, `l`; do not greedily consume arbitrary words. Never remove `27b`, `35b`, `a3b`, arbitrary interior tokens, or version digits. The requested inventory quant reduces to `qwen3.8-hauhau-aggressive-27b`; no fictitious 3.6 match is permitted.
5. Split slash/dash runs as separators, collapse empty tokens. Keep decimal dots **inside tokens** (`qwen3.8`, `6.1`), parameter sizes and dates. Catalog provider namespaces: for comparison remove the first slash component of LiteLLM keys that contain a slash; remove the vendor component from OpenRouter keys. Then remove a leading duplicate family vendor token only for `qwen/qwen...`, `openai/gpt...`, `anthropic/claude...`. Do not strip arbitrary further prefixes or region tags. Bare LiteLLM keys have no namespace removal.
6. Compare full normalized token vectors first. If none, signature search requires exact family/version, all variant tokens, parameter-size tokens (including `a3b`), and dates. Recognize date runs `YYYY-MM-DD`, `YYYYMMDD`, or terminal `MMDD`; normalize first two to `YYYYMMDD`, keep MMDD distinct. A query without a date can match only undated catalog models; a dated query cannot lose or change its date. Family/version is the initial model token (e.g. `qwen3.8`) or initial family plus next numeric version (`gpt`, `6.1`; `claude`, `sonnet`, `5`, `5`). Remaining ordered tokens are mandatory, not stop words. No edit distance, substring family match, cross-version fallback, or size-nearest match.
7. A match must be unique within preferred source. If several provider keys normalize to that vector/signature, select only when **all have identical input and output rates**; tie-break lexicographically by full source-qualified key for determinism. Different rates -> `ambiguous_model`. Only if no primary candidate exists consider secondary. No lexical choice between different prices.

Implementation simplification: steps 5–6 may compare canonical token tuples with date token coalescing rather than building a search engine; token order is retained. There is deliberately no broad removal of `next`, `hauhau`, `aggressive`, or `coder` and no inference of `a3b`. Aliases express those exceptional judgments. This is safer than stripping identity until an arbitrary tariff fits. Test a synthetic `qwen3.8-27b-iq3-gpu-96k-b8k` against a single-candidate catalog as heuristic success; test the real custom IDs with aliases as success and without aliases as unknown/ambiguous as applicable.

## 6. Python interface and arithmetic

Use frozen dataclasses in `pricing.py`; Python consumers import these names. Constructors are keyword-capable. Types below are contractual signatures, not an implementation:

```python
CatalogEntry(source: str, source_key: str, input_usd_per_token: Decimal,
             output_usd_per_token: Decimal, fetched_at: datetime)
Alias(catalog_key: str, kind: Literal["equivalent", "proxy"], note: str)
CatalogSnapshot(entries: Mapping[str, CatalogEntry], revision: str,
                origin: Literal["seed", "cache", "network"],
                warnings: tuple[str, ...])
PriceResolution(api_equivalent_usd: float | None, amount_pico_usd: int | None,
                catalog_key: str | None, method: Literal["alias", "heuristic", "none"],
                alias_kind: Literal["equivalent", "proxy"] | None,
                note: str | None, reason: str | None, source: str | None,
                fetched_at: datetime | None, stale: bool)

def resolve_api_equivalent(model_id: str | None,
                           input_count: int | None,
                           output_count: int | None, *,
                           catalog: CatalogSnapshot,
                           aliases: Mapping[str, Alias],
                           now: datetime) -> PriceResolution: ...

class PricingCatalogManager:
    def __init__(self, data_dir: Path, *, clock, random_uniform, fetch): ...
    def snapshot(self) -> CatalogSnapshot: ...
    def refresh_if_due(self) -> None: ...

def load_seed() -> CatalogSnapshot: ...
def load_aliases() -> Mapping[str, Alias]: ...
```

`data_dir` is the UsageDeck data directory, not its root parent. `snapshot()` is nonblocking, schedules a due refresh, and returns an immutable current view. `refresh_if_due()` performs bounded refresh work for the background worker and synchronous tests. `fetch(url, timeout_s, max_bytes)` is an injectable transport returning bytes; production enforces the fixed-host policy. Alias/seed loaders use package-relative paths, not CWD. No network/disk/clock side effects in `resolve_api_equivalent`; normal unpriced cases never throw. `now` and entry dates are timezone-aware UTC. Constructor/load/transport exceptions are caught by the manager, retained as sanitized warning codes, not HTTP 500.

Both token counts must be known nonnegative integers (bool is invalid). Missing -> reason `tokens_missing`; invalid -> `tokens_invalid`. Resolve identity even when tokens are missing, retaining its method/key metadata; return null amount. Unknown identity -> `model_unknown`, `no_catalog_match`, `ambiguous_model`, or `alias_target_missing` as above. Successful priced result has null reason. `method=none` only when identity was not resolved. Staleness alone does not null an amount.

For each canonical provenance row (the existing daily home/model/provider/attribution row), compute:

`Decimal(input_count) * input_rate + Decimal(output_count) * output_rate`

Quantize **once at that leaf** to USD 0.000000000001 (pico-USD), ROUND_HALF_EVEN, then retain integer pico-USD as the authoritative additive amount. Set `api_equivalent_usd = float(Decimal(amount_pico_usd) / 10**12)`. No rounding at intermediate model/profile/day rollups, no recalculation from merged token counts, no floats for internal sums. Zero-token priced observations and genuinely free rates give numeric zero; unknown never gives zero, including unknown models with zero tokens. No observations -> null (not a known zero observation).

Aggregate sum = sum known leaf integers; null if there are **no known leaves**. A mixed group returns known subtotal plus `partial=true`; unknown leaves are excluded, not priced at zero. Identity-unknown and missing-token rows both appear in the unpriced breakdown with their reason. Existing `missing_fields` and analytics `complete`/`reason` retain their pre-feature meanings; new pricing completeness is separate.

## 7. Wire contract and UI

Add fields only to `/usage` analytics responses. Preserve existing envelope/schema version (additive extension), attribution keys and all prior values. Do not add this to provider subscription meters. Extend the DTO sanitizer's key/enum allowlists so fields survive redaction. Arbitrary alias notes and catalog IDs must still pass existing redaction/length rules and be rendered as **text**, never HTML.

Every `data.rows[i].metrics`, `data.groups[i].totals`, and `data.totals` gains:

```json
{
  "api_equivalent_usd": 0.000001,
  "api_equivalent": {
    "amount_pico_usd": "1000000",
    "priced_rows": 1,
    "unpriced_rows": 0,
    "proxy_rows": 0,
    "partial": false,
    "stale": false
  }
}
```

`amount_pico_usd` is a base-10 integer **string or null**, avoiding JavaScript integer overflow; `api_equivalent_usd` is a finite nonnegative JSON number or null. Row counters count canonical provenance rows, not sessions/tokens; aggregate counters sum leaf counters (so `dimension=repo`, whose leaves are split per repository, may count more leaves than the other dimensions for identical dollars; see analytics-depth-breakdown §6 "Row counters vs dollars"). On a leaf, priced/unpriced is exactly one of (1,0)/(0,1); `proxy_rows` is 1 only for a priced proxy. `partial = unpriced_rows > 0`; empty aggregate has null amount, counters zero, partial false. Row metrics always include both new fields, even when null. The response totals card reads `data.totals.api_equivalent_usd`, never a sum limited to visible top-N.

Each provenance row also gains sibling `pricing`:

```json
{
  "catalog_key": "litellm:azure_ai/gpt-6-luna",
  "method": "alias",
  "alias_kind": "equivalent",
  "note": "Prices as the cataloged Azure AI GPT-6 Luna text tariff; not subscription spend.",
  "reason": null,
  "source": "litellm",
  "fetched_at": "2026-11-21T00:57:39.831243Z",
  "stale": false
}
```

Exactly these eight fields; null metadata follows `PriceResolution`. All times serialize as UTC RFC3339 `Z`. `method` is alias|heuristic|none. Each response gains `data.pricing`:

```json
{
  "catalog_revision": "<sha256 of normalized entries>",
  "origin": "seed",
  "warnings": [],
  "unknown_row_ids": []
}
```

Warnings are sorted unique codes from `pricing_stale`, `pricing_refresh_failed`, `pricing_cache_invalid`, `pricing_cache_unwritable`, `pricing_aliases_invalid`, `pricing_seed_invalid`; no raw exceptions/paths. `unknown_row_ids` contains sorted existing provenance row IDs whose amount is null, across the **filtered authorized response only**. It is the explicit unpriced/unknown bucket, not a second group counted in totals. UI derives reasons and exact model IDs from `rows` and labels it **Unpriced models**. Distinct from an unknown provider. Do not change model group IDs or move unknown rows out of their original groups.

Daily `series[*].points[*]` also gain the same two metric fields, summing the matching leaf amounts by day and group membership. Days with no matching leaves have null API-equivalent even if host analytics has a legacy point. Preserve all existing point fields/series/confidence/spend values. If host profile-All analytics differs from session provenance, compute **only the new fields** from scoped session rows; never scale them to host totals or replace old fields. Expose source incompleteness using existing analytics status; pricing rows do not grant access to other homes.

Service integration freezes catalog and aliases before annotating leaves, then annotates groups/series/totals using existing row membership. Include catalog revision **and alias-file digest** in analytics response-cache identity (alongside existing source/home/window/provider/dimension keys). Refresh becomes visible on the next cache lookup, not a mixture of old totals/new rows. Current request can finish on its frozen old snapshot.

### UI presentation

- Add **API-equivalent ($)** beside, never instead of, existing spend in totals card, by-model rows, All and by-profile rollups. Other/top-N rollup includes every member's amount. Preserve original exact session model ID labels.
- Tooltip: “Hypothetical cost at current base text API rates; not actual spend. Excludes cache discounts, context tiers and extra fees.”
- null renders `—` with reason/unpriced count; numeric zero renders `$0.00`. Never use `value || 0` or truthiness tests. Mixed rollups show the known subtotal with **Partial · N unpriced**. Empty shows `—`, not `$0.00`.
- A row details/tooltip exposes original model ID, resolved catalog key, method, alias note, proxy status and fetched_at/stale. Aggregates expose their constituent rows through `row_ids`; do not choose a single misleading key for a mixed group. Show **Price proxy** for custom proxies, **Stale prices** when applicable, and an expandable **Unpriced models** list with row/model/reason. No hidden unmatched models.
- Display two decimal USD places; nonzero amounts below one cent render `<$0.01` with exact value on hover. Never imply cent-rounded screen labels are authoritative exact arithmetic. Exact totals/parts are available in details using pico-USD; sums conserve at that precision. JavaScript uses supplied totals and BigInt strings for optional exact checks, never sums rounded labels.

### Conservation invariant

For fixed authorized scope, provider filter, window, catalog revision and aliases: sum known leaf pico-USD == sum model groups including Other == sum profile groups == All group == grand total == sum daily parts over the same leaves. Unknowns contribute no amount and counts remain conserved. An all-unknown/empty collection retains null at every relevant total; arithmetic comparisons treat the empty set as no known result, not wire zero. JSON-number comparisons allow ≤ 1e-12 USD serialization tolerance; the integer-string invariant is exact. Rounded screen cents need not sum; do not change leaf values to force them to.

## 8. Implementation/acceptance gates

1. Table-driven tests of all seven **exact recorded IDs** using aliases and real seeded targets. Assert method/key/kind, rates, amount; a heuristic cannot override an alias. Missing alias target cannot silently select another key. Explicitly test cyber-tiel as a visible proxy, not inferred identity.
2. Synthetic heuristic cases: each iq2–iq8/q2–q8, chained hardware suffixes, colon size preserved, dates retained, version/size/variant mismatches rejected, provider-rate ambiguity, identical-rate lexical tie, primary before secondary, free zero vs invalid/missing rate, unknown/missing token vs zero distinction. No cross-version qwen3.8→3.6 fallback.
3. Offline cold start with network disabled resolves seven from packaged seed; valid cache overrides seed per key. Invalid cache/aliases, failed one/both sources, delisting, write-denied/symlink paths, malformed/oversized JSON, duplicate keys, stale seed, jitter and retry bounds; failure preserves prior shown values/timestamps. Tests use injected network, clock and random; never depend on live prices/counts.
4. Conservation over mixed priced/proxy/unpriced, zero and missing tokens; two roots, profile/provider/window filters, Other and daily grouping; exact integer equality and catalog-swap atomicity. No state.db writes; authorized-home attacks stay rejected.
5. Existing spend/parity tests pass unmodified; new regression compares pre/post old-field values and formatted spend byte-for-byte. Existing exact-key schema tests may explicitly add the new keys as this contract requires, **not** relax old field/value assertions or rewrite parity expectations. Verify sanitization carries all new fields without exposing secrets.
6. Packaging: add `pricing.py` to staging backend files and `pricing_data` to staged backend dirs; ensure seed/aliases are shipped in dashboard package with source bytes unchanged. Restage via `scripts/stage_dashboard.py`; ensure desktop canonical `plugin.js` and root mirror still match and consume the same backend wire format (no independent rates/aliases embedded in desktop JS). Run existing packaging suite, staging checks and git-installable-manifest checks; cover new assets explicitly.
7. UI/jsdom tests for null/zero/proxy/stale/partial details in All/model/profile, all seven IDs, Other, and unchanged spend. Guarded node scope with MemoryMax and `--test-concurrency=1`; full backend tests and visual QA at candidate head per parent brief. This design commit does not claim those future feature gates passed.

## 9. Integration map (evidence in existing repository)

- `adapters/session_db.py:47–81`: known-only metrics/null arithmetic; keep old arithmetic unchanged and attach pricing separately.
- `adapters/dimensions.py:63–78,137–144,187–227`: canonical provenance leaves and membership/Other partition. Price before rollup.
- `service.py:329–340,367–437`: request-scoped home binding, response cache, legacy profile-All override and wire assembly. Extend, do not replace, those paths.
- `models/dto.py:610–633`: sanitizer/usage key allowlists must know additive fields.
- `captures.py:10`: established plugin data-dir convention.
- `scripts/stage_dashboard.py:35–37`: backend staging lists need the new module/data dir in the implementation.

These paths are relative to `plugins/usage_deck/` unless prefixed `scripts/`. No feature code, generated assets or production state is changed by this design document.
