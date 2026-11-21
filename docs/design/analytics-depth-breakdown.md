# Analytics depth: time, cache economics, and repositories

Status: implementation contract. Owner: architect, task `t_5ab77058`.
Scope: UsageDeck `/usage` v2 only; additive read-side analytics, no collection or DB migration.

## 1. Evidence and decision refinements

Primary input: scout task `t_e600e05f`, **Recon findings — usage pricing, analytics envelope, session schema, dimensions**, attached as `recon_findings.md` at `/root/.hermes/kanban/boards/usage-deck/attachments/t_e600e05f/recon_findings.md`. References below to R are to that artifact's numbered lines. R:11–48 establishes pricing; R:52–111 the v2 envelope; R:115–142 nullable session columns; R:147–184 dimension seams. The legacy Hermes `/api/analytics/*` router is NOT this feature's endpoint.

Source baseline for the design branch: UsageDeck `c4a662d2e9b00644d9ddd4a5bc332f381a3a2680`. Main checkout currently differs and is older; do not infer absence of v2 from that checkout. Also inspected sibling integration `t_c08b2ccc` at `d1bfff6fe67576783bcd5b5a0e17e2bf4b8a6284`, especially `plugins/usage_deck/adapters/session_db.py:327–356`, `models/dto.py:351–372`, and `dist/index.js:1280–1419`. File paths below are relative to `plugins/usage_deck/` unless otherwise stated.

Pricing semantics pinned to Hermes core `3b421578588243f920886e6fe79a6faa87ea026e`, `agent/usage_pricing.py:156–214,649–701,709–757`. This is a source-semantic pin, NOT a claim that these rates are today's public prices. Implementation packages a local projection of those snapshot rows; runtime must not import Hermes core or fetch pricing over the network for this feature.

Binding decisions D1–D6 are retained with these explicit refinements:

- **D1:** a UTC weekday/hour aggregate alone cannot be converted to local time correctly across dates, DST, or half-hour offsets. Ship epoch contributions alongside the required UTC grid (§4); never rotate the grid by a single offset.
- **D2:** basename collisions intentionally combine, but all distinct full paths must remain inspectable. Add `full_paths` alongside nullable singular `full_path` (§6). A literal repository named `Unscoped` is not the missing-repo bucket.
- **D3/C1:** old `estimated_usd` uses legacy SQL `COALESCE`, and profile-All may use host analytics rather than session rows. Do not silently change those values to impose a new null doctrine. New `depth_totals` and `model_rollup` supply a common, null-preserving session basis for the new conservation invariant (§2, §6). Where legacy provenance has complete observations, its counters/cost agree as well. This explicit source distinction preserves the brief's profile parity requirement.
- **D4:** the written dollars/tokens quotient is dollars **per token**. Multiply by 1,000,000 for the promised dollars/Mtok. Cache discount is per exact provider/model snapshot row, not a global 0.1 constant. Reasoning is a recorded counter in this diagnostic denominator, not a new billable category (§5).
- **D5:** append cards and reuse the existing dimension/chart components; never redesign current charts (§7).
- **D6:** API-equivalent integration lands first; rebase this implementation and regenerate staged files afterward (§8). API-equivalent money is never substituted for recorded cost in cache economics.

These refinements resolve contradictions, not optional alternatives. Implementers/verifiers use this document's names and formulas.

## 2. Wire contract: additive v2

Keep `meta.schema_version = 2`, existing URLs, parameters, envelope keys, and existing field values. Add `repo` to `dimension` everywhere the enum is checked. No change to provider attribution, authorized-home binding, time window predicate, or exact recorded model IDs.

Extend the v2 DTO encoder, decoder, sanitizer and strict key allowlists **together**. The recon's suggestion to bump the version for a new data key is superseded: this brief explicitly requires additive v2. Current source uses exact-key validation, so an old strict client cannot decode new fields without updating. Do not claim otherwise. Ship updated halves together; the new decoder accepts the baseline v2 shape with these additions absent, interpreting absent additions as null. It also accepts sibling pricing additions, and still rejects unrelated unknown fields/invalid types. Preserve strict validation of all baseline keys. Absence means older server, not a zero observation.

### Additions to `data`

| Key | Wire type and scope |
|---|---|
| `durations` | `DurationStats | null`, all filtered authorized sessions |
| `duration_samples` | nonnegative integer or null, number of valid finished durations |
| `heatmap` | `Heatmap | null`, same filtered session set |
| `cache_metrics` | `CacheRow[] | null`, every exact model/provider pair, no top-N |
| `repo_rollup` | `RepoRow[] | null`, every basename plus Unscoped |
| `model_rollup` | `ModelRow[] | null`, every exact model/provider pair, no top-N; conservation reference |
| `depth_totals` | `DepthTotals | null`, conservation grand total |
| `depth_status` | `DepthStatus | null`, source and missing-observation disclosure |

These eight keys are emitted by the updated server even when null. Objects/arrays being null means the source/read/feature is unavailable. A successful empty read emits empty arrays, an empty grid and `depth_totals.sessions = 0`; no recorded token or cost observation remains null, not fabricated zero. Only emit observed repo groups, with the explicit Unscoped row included even if empty (§6).

Each existing `data.groups[]` additionally gains `durations: DurationStats | null` and `duration_samples: integer | null`. These describe the group's true session membership, including all members of model Other. All/profile/model/repo duration percentiles are computed from samples, never means or percentiles of group percentiles. Do not add duration stats to each daily point or modify existing chart code.

`DurationStats` has exactly `{p50, p90, p99, mean, max}`, each seconds as finite nonnegative JSON number or null. With no valid duration samples emit the object with all five null and `duration_samples = 0`; source unavailable is object null and samples null. No NaN/Infinity/bool-as-number. Counts are nonnegative integers; money and ratios finite nonnegative numbers or null; percentage <=100. All scalar additions, including paths and model/provider IDs, are nullable where data is unrecorded. Required structural IDs in a present object are not nullable.

`DepthTotals` has exactly `{sessions, total_tokens, total_cost}`. `ModelRow` has exactly `{model, provider_id, sessions, total_tokens, total_cost}`. IDs use existing sanitized exact recorded model and resolved provider values; null IDs remain null and form honest unknown groups. Do not normalize a recorded model to resolve a price.

`DepthStatus` has exactly `{partial, excluded_started_at, missing}`. `partial` is true for an unavailable authorized source, invalid starts, or any missing observations. `excluded_started_at` counts rows rejected for invalid starts in successfully scanned sources (null if not determinable). `missing` maps the following fixed names to counts of included sessions lacking usable values: `input_tokens`, `output_tokens`, `estimated_cost_usd`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `duration`. Counts are integers; unavailable scans use null counts. Existing `sources`, `complete`, `meta.partial/errors` continue to disclose home failures. Missing optional observations alone do not fail the HTTP request or invent a new existing error enum.

### Shared null arithmetic and token meaning

New rollups use a known-only sum: discard null operands; if none remain, return null, otherwise sum including recorded zeros. Never `value || 0`. Mixed known/missing results are known subtotals disclosed by `depth_status`; every ratio requires complete paired operands for its population (§5).

For these new conservation/heatmap fields, `total_tokens` means **known input + output**, consistent with existing model ranking/charts. Sum known components across sessions; an all-missing token population is null. It does NOT mean billable tokens and does not add cache/reasoning counters. Those remain separate diagnostic fields; reasoning can overlap output. `total_cost` is the known-only sum of **recorded** `estimated_cost_usd`, not API-equivalent, never inferred from a legacy coalesced zero. An explicit recorded 0 is real zero for this display (including a subscription-included session); do not invent dollars spent from it.

## 3. Read path, duration algorithm, and interfaces

Retain existing daily query/provenance aggregation and its order/IDs unchanged. Adding repo to its GROUP BY would split legacy leaves and alter pricing leaf counters/row IDs, violating parity. Instead, in the **same read-only per-home transaction**, scan a second projection for this feature. No independent re-open with a different snapshot and no per-session N+1 query.

Projection: `started_at`, `ended_at`, `last_activity_at`, `model`, optional `provider`, `billing_provider`, `git_repo_root`, `input_tokens`, `output_tokens`, `estimated_cost_usd`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `api_call_count`; absent optional columns project SQL NULL after PRAGMA inspection. `last_activity_at` is not an end-time substitute. Use the existing bound cutoff and exact predicate `started_at > cutoff`; do not introduce a new end-of-window exclusion for one dimension. Invalid/non-finite/non-numeric starts cannot bucket and are excluded/disclosed, never assigned to now. Negative/invalid/non-finite token or cost observations become missing in the NEW projection; old metrics stay untouched. Recorded 0 remains known. Booleans are invalid numeric observations.

Proposed internal seam names are normative:

- New `adapters/analytics_depth.py`: immutable `SessionFact` and pure `build_depth(facts, *, dimension, rate_table)` returning the new data members plus per-group duration membership. `SessionFact` carries home/profile holder and already-attributed provider alongside the projection; no auth data or unapproved homes.
- Extend `SessionDbReader`/`HomeRead` with optional `facts` from its existing transaction. Deduplicate physical DB identity and honor authorized profile binding exactly as the baseline reader does. A failed optional projection makes depth unavailable for that source, not legacy usage unavailable if its existing query succeeded. Mark partial and retain other sources' known subtotals.
- Extend `dimensions.partition` with repo grouping built from `SessionFact` membership; existing branches and legacy rows remain untouched. For repo only, create additive repo provenance leaves from facts grouped by existing leaf keys plus repo key; use them for repo group/series membership and preserve any sibling pricing annotations. Do not reuse the old non-repo row IDs for these newly split leaves.
- Service scopes facts with the **same** home/window/provider filter and attribution resolver as usage before building any new field. All dimension choices see the same depth totals. Use the existing per-request snapshot/cache path; include depth rate snapshot digest in cache identity. Never rescan other homes for profile mode.

Duration sample = `ended_at - started_at` only if both finite numbers and end >= start. Open sessions, missing/invalid ends and negative intervals are excluded; a zero-length completed session counts. Never use last_activity_at or current time for an open session. All samples, including outliers, remain; no clipping. Sort samples `x[0..n-1]`; percentile at p uses linear interpolation `h=(n-1)*p`, `x[floor(h)] + (h-floor(h))*(x[ceil(h)]-x[floor(h)])` for p=.50/.90/.99. Mean is sum/n and max last sample. Keep full precision until display. Example samples `[0,10,20,30]` yield p50=15, p90=27, p99=29.7, mean=15, max=30.

## 4. Heatmap and intraday detail (D1/C3)

`Heatmap` has exactly `{grid, starts}`. `grid` is seven arrays of 24 cells, Monday=0 through Sunday=6, hour 0..23. Each cell has exactly `{sessions, total_tokens}`. For an available empty cell sessions=0 and total_tokens=null (no token observations); observed all-zero tokens give total_tokens=0. Do not zero-fill missing observations. A missing/unavailable entire source uses `heatmap=null` when no source can be read; mixed-source subtotals are marked partial.

Bucket a finite epoch using UTC conversion (`datetime.fromtimestamp(started_at, timezone.utc)`): weekday=`.weekday()`, hour=`.hour`. Each session contributes **once** at its start, with its whole recorded input/output token contribution. Neither ending at midnight nor spanning days splits its count or tokens. This is start-cohort activity, not token event-time distribution.

`starts` is a sorted ascending array of `{started_at, sessions, total_tokens}`. `started_at` is the original finite numeric epoch seconds, not milliseconds, not a formatted local date, and not rounded to the UTC hour. Combine facts only with identical epoch values; aggregate with the same known-only arithmetic. No session IDs, repository paths or model IDs in this array. Its aggregate equals grid aggregate. It is a required sufficient-statistic supplement, not new data collection. Never truncate it silently: it is one record per distinct start within the filtered 7/30/90-day population. Cache alongside the response; 7×24 is the rendered cell count, not a license to discard start-date information. `starts` also supplies the additive intraday detail: selecting a heatmap cell opens a day/hour table derived from matching start epochs with sessions/tokens, without replacing existing daily charts.

Browser rendering re-buckets `starts`, NOT `grid`: `d=new Date(started_at*1000)`, row=`(d.getDay()+6)%7`, column=`d.getHours()`. Browser timezone rules for that actual instant govern the result; never use today's offset or a server-local date string. Sum each contribution once. A timezone change/browser refresh recomputes from cached epochs; no timezone is persisted or sent to a DB. UTC grid remains the API's deterministic test/debug reference. If an old/malformed response lacks starts, show “Local heatmap unavailable” rather than silently mislabel a rotated UTC grid as local.

DST acceptance examples (instants and local civil labels, from the platform IANA timezone database):

| UTC start | UTC cell | Browser zone | Local cell/label |
|---|---|---|---|
| 2026-03-08T06:59:00Z | Sun 06 | America/New_York | Sun 01:59, before spring gap |
| 2026-03-08T07:00:00Z | Sun 07 | America/New_York | Sun 03:00; no 02:00 cohort from these samples |
| 2026-11-01T05:30:00Z | Sun 05 | America/New_York | Sun 01:30, first occurrence |
| 2026-11-01T06:30:00Z | Sun 06 | America/New_York | Sun 01:30, second occurrence; same cell, count both |
| 2026-11-21T23:45:00Z | Sat 23 | Asia/Kolkata | Sun 05:15, fractional offset crosses day |
| 2026-11-21T23:59:00Z | Sat 23 | UTC | Sat 23, even if end is Nov 22 00:10Z |

Test fixed epochs using `zoneinfo`/browser TZ overrides; do not approximate DST with a hand-coded month rule. A local fold cell can contain two different UTC hours; detail includes ISO instant plus local offset to disambiguate. Both repeated cohorts count, neither is deduplicated by local text.

## 5. Cache economics (D4/C4)

`CacheRow` has exactly these keys:

- `model`, `provider_id`: exact attributed pair, nullable; sorted deterministically null-first then lexical model/provider.
- `hit_rate_pct`, `cached_input_tokens`, `uncached_input_tokens`, `reasoning_tokens`, `effective_cost_per_mtok`: the required nullable numerical fields.
- `cache_write_tokens`, `output_tokens`, `reasoning_share_pct`, `discount_multiplier`: nullable diagnostics.
- `rate_source`: snapshot pricing_version string or null; `reason`: null or `missing_usage`, `missing_cost`, `unknown_discount`, `zero_denominator`, `incomplete_source` (first applicable in that order).

No Other: list all model/provider pairs in a scrollable table. Raw aggregate token fields are known-only sums with missing counters disclosed. Ratios are computed from raw records, never average session percentages or average model rates. For a pair with any missing required operand in its included population, the affected ratio is null. For a partially unreadable authorized population, display known raw subtotals but ratios are null with `incomplete_source` unless an earlier field-level reason applies.

Core normalizes uncached input separately: `normalize_usage` subtracts cache read/write from OpenAI prompt totals but uses Anthropic uncached input directly (`usage_pricing.py:667–674`). Session counters are already recorded canonical values: **do not subtract cache reads a second time**. Naming: I=input_tokens (uncached), O=output_tokens, R=reasoning_tokens, W=cache_write_tokens, C=cache_read_tokens, K=recorded estimated_cost_usd.

- cached_input_tokens = sum(C); uncached_input_tokens = sum(I).
- hit_rate_pct = `100 * sum(C) / sum(I + W + C)`. Writes are input misses, not hits. Require every I/W/C operand known; denominator zero -> null. This is the recorded bucket hit signal, not proof of provider cache billing or savings.
- reasoning_tokens = sum(R). reasoning_share_pct = `100 * sum(R) / sum(O)` only when every R/O is known, O>0 and 0<=R<=O for every included session; otherwise null. Reasoning is commonly an output subset; never divide by O+R and call it output share. Records violating the subset relationship still retain their raw counters; do not clamp the ratio to 100.
- discount_multiplier `d = cache_read_cost_per_million / input_cost_per_million` from the **same exact provider/model row** in the pinned official snapshot. Need finite input rate >0 and cache rate >=0; missing/ambiguous/zero-input tariff -> null. R:23–27 examples: GPT-5.6-sol d=.10, GPT-4o d=.50, DeepSeek flash d=.02. There is no universal discount constant.

Package `pricing_data/cache-discounts-v1.json` as `{core_commit, entries}` with entries `{provider_id, model, pricing_version, input_cost_per_million, cache_read_cost_per_million}`; rates are decimal strings or null. Expand core tuple aliases into **explicit exact model entries** at build time from `_SNAPSHOTS`, including both missing and zero cache-rate cases. Preserve the source snapshot provider string as provider_id; use the same normalized attribution identifier as the existing provider resolver. No fuzzy matching, stripping provider prefixes, inference from API-equivalent alias proxies, current catalog lookups, or independent JS rates. If attribution yields a provider absent from the snapshot, discount is unknown, not a guessed cross-provider tariff. Include the core pin and snapshot digest in documentation/fixture tests; update deliberately when semantics change. Tier/request fees and provider overrides are not reconstructible from aggregate session rows; this diagnostic is explicitly **base-snapshot cache weighting**, not a reconstructed invoice.

For a model/provider pair:

```
D_tokens = sum(I + O + R + W + C*d)
effective_cost_per_mtok = 1_000_000 * sum(K) / D_tokens
```

Use Decimal for rates, money sums and denominator, no intermediate cent rounding. Require complete I/O/R/W/C and recorded K for every included session; otherwise effective rate=null (do not divide known costs by a larger incomplete population). If d is unknown but **all C are explicitly zero**, the cache contribution is provably zero: compute D without it while exposing discount_multiplier=null. If any C>0 and d unknown, effective rate=null/unknown_discount. Denominator zero -> null even with K=0. Nonzero denominator and fully recorded zero cost -> numeric 0; label “recorded-cost basis”, not “free public API”. Discount multiplier itself remains available even when usage is missing.

Core's cost loop bills I/O/C/W and **does not bill R separately** (`usage_pricing.py:741–755`, R:31–38). D4 explicitly includes R anyway; follow that denominator but label the value **Effective recorded $/Mtok (weighted diagnostic)** with tooltip: “Recorded cost divided by base-cache-weighted recorded counters. Reasoning may overlap output; not a tariff, invoice, or measured savings.” Do not modify core pricing or add a reasoning charge. Cache-write tokens have weight 1 in D4 despite a 1.25x write tariff; no secret adjustment.

### Worked example — normative test vector

Pinned OpenAI `gpt-5.6-sol`: input=$5/M, output=$30/M, cache read=$0.50/M, cache write=$6.25/M; d=.50/5=.10. One complete record: I=1,000,000, O=100,000, R=20,000, W=100,000, C=2,000,000.

Recorded K for this fixture is the core-style estimate: 5 + 3 + .625 + 1 = **$9.625** (R adds no separate charge). D=1,000,000+100,000+20,000+100,000+200,000=**1,420,000 weighted tokens**. Effective=$9.625/1.42=**6.778169014084507… $/Mtok**. Hit rate=100×2,000,000/3,100,000=**64.51612903225806…%**. Reasoning share=20,000/100,000×100=**20%**. The raw dollars/tokens quotient is .000006778169…, not $6.78 until scaled to Mtok. UI rounds only for labels; tests compare Decimal expressions or numeric error <=1e-12, not rounded cents.

## 6. By-repo and conservation (D2/D3)

A repository key is the lexical basename of a nonempty `git_repo_root`, after trimming surrounding whitespace and trailing path separators. Treat both `/` and `\\` as separators for recorded cross-platform paths, with no filesystem resolution, stat, git command or cwd fallback. Preserve case; no symlink canonicalization. Null/empty/whitespace-only/separator-only paths map to the distinguished missing key. `cwd` and `git_branch` do not invent a project.

Present `RepoRow` fields: `{repo_basename, full_path, sessions, total_tokens, total_cost, full_paths}`. Missing key renders `repo_basename="Unscoped"`, full_path=null, full_paths=[]; always include it, with sessions=0/tokens=null/cost=null when empty. Real basename `Unscoped` is distinct internally. A one-path basename has full_path equal to the sanitized complete path and full_paths=[that path]. A collision has full_path=null and sorted unique full_paths containing every sanitized contributing path; UI detail says “N paths” and expands them. Do not choose one misleading path or split the basename into separate groups.

Paths are sensitive local telemetry. Send only to the already-authorized local analytics caller, through existing secret redaction plus a dedicated nullable path validator (length <=4096, reject NUL/control characters). Identity/grouping uses the original lexical basename before display redaction; retain separate groups even if their sanitized labels collide. Redacted/rejected detail paths may be null/omitted from full_paths, but never drop their metrics or expose unredacted text in errors. Render path/model strings as text nodes, never HTML. Do not log paths or put full paths into group IDs. Offline display reads the returned/cached data; no new network dependencies.

Repo group ID grammar is `repo:null` for missing, otherwise `repo:` plus a JSON string of the original basename (e.g. `repo:"usage-deck"`); extend existing group-ID validation to those alternatives. Literal `Unscoped` is `repo:"Unscoped"`. If the existing secret redactor would change the original basename, or its length exceeds the existing ID string limit, use `repo:sha256:` plus the 64 lowercase hexadecimal SHA-256 digits of the original UTF-8 basename instead. This exact additional ID grammar must be accepted by the validator. Never serialize a raw secret-bearing basename in an ID. Membership is determined before display redaction and cannot merge unrelated keys. No raw full path enters an ID. Repo groups are not top-N: `top_n=null`, `is_other=false`. Sort descending known input+output total (unknown last), then original basename lexical, missing bucket last. Repo row order follows group order. There is no Other for repo or cache tables.

For dimension=repo the current API `groups`, `series`, `rows` use repo membership. Aggregate facts into repo provenance leaves keyed by `(holder, legacy local day, exact model, attributed provider, repo key)` and group totals, preserving legacy UsageMetrics formulas including its old cost behavior **for this new view only**. Add nullable `repo_basename` to these repo provenance rows (absent/null on old dimensions), plus `repo_paths` for sanitized detail (array or null); allowlists must include both. Existing row IDs in all/profile/model remain byte-identical. Repo-only row IDs must encode repo membership with the existing deterministic ID utility; no collision across different repo keys. If pricing integration is present, annotate split repo leaves before summing; amounts conserve against the other dimensions when every split part has complete recorded tokens, while the row counters (and, with one-sided missing token counts, the amounts) can differ for repo because leaves split. See "Row counters vs dollars across dimensions" below for the exact conditions and the known missing-token limitation.

**Row counters vs dollars across dimensions (reconciled, t_045ea424).** `totals.api_equivalent.{priced,unpriced,proxy}_rows` count the leaves of the response's own `rows[]` (`priced_rows + unpriced_rows == len(rows)`; `pricing.unknown_row_ids` lists exactly the unpriced ones). all/profile/model share the legacy leaves, so their whole block is identical. `repo` splits a legacy leaf into one leaf per repository, so its counters can differ from the legacy ones. Counter parity across dimensions is **not** an invariant. Dollar parity holds under one stated condition: **every split part has complete recorded tokens** (input and output both recorded; recorded zeros count as recorded) or no tokens at all. Under that condition each repo part is priced once, the integer parts sum to the whole (per legacy leaf: sum of known part amounts == legacy leaf amount), and the repo counters exceed the legacy ones by exactly the extra leaves. Three cases:

1. **Recorded-zero part (dollar-neutral; the live observation).** A split part whose sessions recorded 0 input and 0 output tokens is a *priced genuine zero* (api-equivalent-pricing §6), so it adds `priced_rows +1`, and `proxy_rows +1` when the model resolves through a proxy alias, and adds no dollars. This is the live-machine `priced +1 / proxy +1` observation (window=90: on 2026-11-07 one `qwen3.8-flash-next` session in `hermes-agent` recorded 0/0 tokens while the other 88 sessions of that day/model/provider leaf were unscoped). Disposition for the finding: correct by design; no product change.
2. **No-token part (dollar-neutral).** A split part whose sessions recorded no tokens at all (both counts missing) is unpriced (`tokens_missing`) and adds `unpriced_rows +1`, while the legacy merged leaf prices the known sums of its siblings, which equal the priced siblings' sums. This can flip `partial` to true for repo only; no money moves.
3. **One-sided missing counts (known limitation; dollars DIFFER).** If a split part has input recorded but output missing, or the reverse, the legacy leaf sums each token column independently over the known values (`sum_known`) and prices the merged pair, while a repo leaf is priced only when its *own* input and output sums are both recorded (`pricing._token_problem`, `tokens_missing`). The same data then prices differently per dimension, and the repo count of priced leaves can be *lower*. Example (same day/model/provider, two repos): parts (10, missing) + (missing, 5) price to `10160000` pico under all/profile/model and to null (priced 0, unpriced 2) under repo; parts (10,5) + (100, missing) price to `35560000` under all and `10160000` under repo, where the complete control (10,5)+(100,5) conserves at `43180000`. This is existing behavior in the legacy totals path and is **not** changed by this reconciliation (profile-mode and legacy-dimension values must not shift); remediation, if wanted, is a separate decision (price the merged known sums in repo, or stop merging by column in legacy).

Pinned by `tests/test_api_equivalent_row_counts.py` (cases 1 and 2 as reconciliation, case 3 as an asserted known divergence so any change is loud).

**Conservation is over one identical authorized session set:**

```
known_sum(repo_rollup.sessions) == known_sum(model_rollup.sessions) == depth_totals.sessions
known_sum(repo_rollup.total_tokens) == known_sum(model_rollup.total_tokens) == depth_totals.total_tokens
known_sum(repo_rollup.total_cost) == known_sum(model_rollup.total_cost) == depth_totals.total_cost
```

`known_sum` returns null on an all-null/empty known population; zero sessions in successful empty scope is the explicit structural exception. Empty synthetic Unscoped contributes no token/cost observations. Assert integer equality, null equivalence, and Decimal money equality before JSON float conversion (wire tolerance <=1e-12 USD). With a failed home, the invariant holds on the same observed subset and `partial=true`; never imply completeness. Hidden chart legend entries are presentation only and do not alter API totals. Compare existing input/output/sessions against old model totals on complete fixtures as an extra guard. Do not sum cost labels, group percentiles, percentages, or top-N without Other.

## 7. UI placement and half parity (D5)

Preserve current analytics controls, totals, charts and daily tables in order and appearance. Dimension control order: All / By profile / By model / **By repo** for machine mode; All / By model / **By repo** for bound profile mode (do not expose other profiles). Reuse current provider/window selectors; changing either refreshes all additions. Repo names appear in the existing grouped chart/table machinery, with nullable full-path detail below the label and expandable collision paths. Adapt labels to “Repositories”; do not fork/rewrite chart plotting algorithms.

Existing session summary/totals area gains a subordinate line **Completed session duration**: p50, p90, p99, mean, max, and valid sample count. Show seconds or human-readable time with exact seconds on hover; no null-to-zero. Grouped summary rows expose that group's five stats and sample count in an expandable detail, not a replacement chart.

Immediately **after the existing chart/summary/data-table block** and before the existing provenance details (where separable), append:

1. Full-width **Activity by start time (local)** card: Monday–Sunday rows, 00–23 columns, default intensity=sessions, local toggle Sessions/Tokens. Tooltip and accessible cell label contain both sessions and known input+output tokens, local weekday/hour, plus partial marker. Null token intensity has a distinct unavailable style, not the zero color. Cells keyboard-focusable; selecting one opens the intraday start-cohort table from §4. Show browser zone in card caption and explanation that sessions are assigned once at start.
2. Full-width **Cache economics · recorded-cost basis** card: scrollable table with exact Model, Provider, Hit %, Cached input, Uncached input, Cache writes, Reasoning, Reasoning share, and Effective recorded $/Mtok; row detail includes output, multiplier and pinned rate_source. No top-N and no price fetch. Include the diagnostic caveat from §5. Null=— (em dash), recorded zero=0 or $0.00, percentages two decimals, cost four decimals with exact value tooltip. Show missing/partial status without replacing legacy status cards.

Implementation location: canonical dashboard `dist/index.js` (and its existing styles), canonical desktop `desktop/plugin.js`; root `plugin.js` is its staged mirror. Desktop order, labels, bucket conversion, null handling and formulas match the dashboard. Reuse backend numbers; no JS recomputation of cache money. Do not hand-edit `dashboard/` staged copies. Stage via `scripts/stage_dashboard.py` and the repository's existing desktop mirror procedure. New decoder against old server hides new cards or shows a bounded unavailable message; never invent an Unscoped zero-spend portfolio.

## 8. Sibling coordination / merge order (D6)

Sibling brief `t_1c0734e0` is governed by `docs/design/api-equivalent-pricing.md` (design head `ea426edeae72bc837f415bb012f121cbdab58a02`); its integration is `t_c08b2ccc`, resolver `t_9938fe21`. Its catalog aliases represent hypothetical cloud equivalents and intentionally exclude cache discounts. This design's discount snapshot is separate; do not overload its seed schema or API-equivalent amount.

Unavoidable overlap: `models/dto.py` (dataclasses, all shape/key/enum/redaction checks), `adapters/dimensions.py` (membership), `service.py` (scope, cache key and envelope), `dist/index.js`, `desktop/plugin.js`, root `plugin.js`, `scripts/stage_dashboard.py` (new module/data assets), packaging/frontend/backend tests and generated `dashboard/**`. `adapters/session_db.py` adds facts here; sibling currently leaves its SQL unchanged. Treat the whole generated dashboard tree as a hotspot, not independent hand-maintained edits.

**Order:** resolver -> API-equivalent integration -> depth implementation rebased onto that result -> combined verifier/regression run. Dispatch owns actual merge scheduling; neither author merges. If depth work starts before sibling merges, keep independent `analytics_depth.py`/tests/design work local and postpone shared-file integration until rebase; do not cherry-pick a stale generated tree on top. Union allowlists, preserve `data.pricing`, all `api_equivalent*` fields and `row.pricing`, include both rate/catalog revisions in cache identity, and regenerate staging once from integrated canonical sources. Add repo to API-equivalent aggregation with no changes to its all/profile/model amounts. Do not land two PRs with one silently overwriting the other's additions.

## 9. Builder/verifier test contract

This document defines expected tests; its delivery does **not** claim feature tests passed.

1. **Conservation fixture:** at least two homes; >8 models (exercise model Other); two distinct paths with the same basename; literal `Unscoped` repo; null/empty/whitespace roots; zero/missing input, output and cost; partial sources; unknown provider/model; open/zero/negative durations; multiple providers per model. Across all/profile/model/repo and provider filters assert §6 identities, no dropped or duplicated sessions, and path collision behavior. Missing-only money stays null; recorded 0 stays 0. Include unknown-discount models in visible cache rows. Run live-union comparison on authorized homes with source identities captured; compare identical observation sets, not mutable reads at different times. Preserve exact recorded inventory IDs.
2. **Duration:** test the `[0,10,20,30]` vector, n=0/1, even/odd n, zero, fractional epochs, invalid ends, open sessions and Other's raw-sample percentile. Nulls excluded from denominator; no last_activity fallback.
3. **Heatmap/DST:** table-driven UTC and browser-local tests for every row of §4, plus epoch zero, negative valid epoch conversion where supported, fractional seconds, invalid epoch, weekday wrap, distinct dates whose same UTC weekday/hour have different DST offsets, half/quarter-hour zones, fold conservation and cross-midnight end. Assert grid 7×24; grid and starts sum identically; local regrouping conserves and does not mutate UTC data. Test accessible labels/intraday detail in both halves.
4. **Cache formula:** package source-pin assertion, exact row rates and d for .10/.50/.02 cases; normative $9.625 / 1.42 vector; writes included in misses; no second input subtraction; R not separately priced; 1e6 unit factor; weighted sum not average of ratios; absent/zero tariff, unknown route, zero-read exception, missing/zero usage/cost, denominator zero and incomplete-source behavior. Reasoning overlap violation -> null share, raw tokens retained. No new network calls under offline test.
5. **Parity regression:** run existing all/profile/model parity tests unmodified in value assertions. Project candidate JSON onto all baseline keys and compare serialized values/row IDs/order byte-for-byte for fixed data/time; compare legacy chart/table subtree and formatted spend strings identically after excluding only newly inserted controls/panels/detail lines. Whole response/DOM byte equality is impossible with additive fields/cards: this is the explicit interpretation of C6, not permission to alter old values. Exact-key assertions may whitelist declared additions, not weaken checks. Include bound profile-All host-override fixture and sibling API-equivalent values unchanged.
6. **Read-only/authorization:** fixture trace denies all writes; open every state DB using existing mode=ro/query_only, same deduplication and bound-home checks. Gate-6 checksum before/after across every authorized home; runtime agents can concurrently mutate live DBs, so isolate fixtures or record writer activity rather than label an uncontrolled live checksum change a plugin write. Check no repo-path filesystem traversal, no symlink escape, secret redaction, HTML/control-character path attacks and basename/ID collision preservation. Do not print private paths into public evidence.
7. **Backend/frontend/packaging:** full backend suite, focused new tests, existing packaging **31 OK baseline** at minimum with explicit coverage for `analytics_depth.py` and `pricing_data/cache-discounts-v1.json`, seeded-data presence and canonical/staged byte equality; `scripts/stage_dashboard.py --check --require-backend` green. Frontend jsdom through the installed guarded node wrapper, `--test-concurrency=1`, MemoryMax scope; one suite at a time, close every jsdom window, never dump entire DOM nodes in assertion failures. Recent sibling test leak was an assertion diffing a whole jsdom node; assert booleans/short strings instead. Record command, SHA, test totals, skips, peak memory and actual results. Both halves' unit/UI tests and visual spot-checks must include By repo, local heatmap, durations and cache table. Run cold/offline cached render with no new network traffic. Packaging counts can grow, never claim “31 OK” from an unexecuted suite.

## 10. Delivery boundaries

Design-only changes belong on `docs/analytics-depth-breakdown`, path `docs/design/analytics-depth-breakdown.md`, before feature implementation. Builder implements `t_5ce577c8`; verifier `t_8bb35d3e` tests the integrated candidate and records actual outputs. No feature code, host pricing changes, deployment or merges are authorized by this design card. Publication/credential/merge ordering is dispatch-owned. Any implementation deviation affecting these interfaces/formulas must return for a cited design decision, not be independently invented.
