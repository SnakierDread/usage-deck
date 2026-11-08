# UsageDeck desktop half — implementation contract

## 1. Scope, authority, and identity

This document is the desktop implementation/QA contract, ratified 2026-11-08. Section numbers are stable handoff references. Requirements below are normative; SDK behavior is distinguished from UsageDeck choices.

- Package/plugin id: `usage-deck`; display name: `UsageDeck`; route: `/usage-deck`.
- The sole shipped desktop source is `plugins/usage_deck/desktop/plugin.js`: plain uncompiled ESM, default-exporting a `HermesPlugin` object, no build, bundler, JSX syntax, auxiliary runtime files, or top-level desktop package.
- Import specifiers are restricted to `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime`. Use `jsx`/`jsxs` from `react/jsx-runtime`. Import the SDK as a namespace called `ui` for kit calls such as `ui.Button`/`ui.Badge`; named SDK imports for hooks/state are also allowed. React hooks may come from `react`; data, atoms and query hooks come from the SDK. No globals, app-internal imports, invented SDK exports, or React-element shims.
- No imports, embedding, iframe, stylesheet loading, or evaluation of `dist/` or `dashboard/dist/`. Dashboard source may be inspected for semantics, not reused as a runtime dependency. The only shared operational logic is the existing backend API.
- Backend baseline is commit `e2323c3`: the three GET routes and all Python remain unchanged. No new routes, credentials, grants, upstream probes, or runtime network destinations. This work does not absorb later adapter enhancements without a separate decision.
- Builder diff: only `plugins/usage_deck/desktop/plugin.js`. Verifier-owned harness additions under `tests/desktop/` are a separately recorded QA deliverable, not permission to widen the builder diff. This design card changes documentation only.

Primary references at baseline: `docs/reference/desktop-plugin-sdk.md` (SDK snapshot, especially lines 150–215, 297–321, 1386–1406, 2067–2216, 2323–2397); `plugins/usage_deck/plugin_api.py`; `service.py`; `models/dto.py`; dashboard `dist/index.js`; existing `DESIGN.md` §§2–6, 12–13. Where this spec narrows presentation it must not change their wire vocabulary.

Supplemental SDK evidence for connection-safe reactivity, not a backend contract change: NousResearch/hermes-agent commit `8202da899b84b6f5783dbf098ddd060b5404a8d6`, `apps/desktop/src/sdk/index.ts:574–575,1898` exports `host.state.connectionId` and `useGatewayActivationEpoch`; `src/store/gateway-activation-epoch.ts:19–30` explains the reactive epoch. `src/contrib/plugin.ts:396`, `src/api/plugins.ts:73–87`, and `src/api/client.ts:713–754` establish REST delegation/routing. Builder/verifier must verify those exports on their target desktop version; §4.5 defines the safe older-build behavior. Do not mistake the snapshot's abbreviated `host.state` list for an exhaustive export list.

## 2. Registration and lifecycle

### 2.1 Contributions

Default export is `{ id: 'usage-deck', name: 'UsageDeck', defaultEnabled: false, register(ctx) { … } }`. `register` is synchronous and installs exactly these two permanent contributions through two `ctx.register` calls:

| Local id | Area | Payload |
| --- | --- | --- |
| `page` | SDK `ROUTES_AREA` | `data: { path: '/usage-deck' }`; `render` mounts the page with this registration's `ctx` |
| `nav` | SDK `SIDEBAR_NAV_AREA` | `data: { path: '/usage-deck', label: 'UsageDeck', codicon: 'graph' }` |

Let the host namespace ids (`usage-deck:page`, `usage-deck:nav`) and stamp provenance. Do not register a parallel pane, status item, settings entry or palette action. The sidebar uses the normal contributed-row ordering; dashboard `after:models` is not a desktop contribution API.

### 2.2 Ownership and teardown

The host owns contribution removal on disable/unload/reload. Every plugin-owned store subscription and other cleanup is registered with `ctx.onDispose`; component subscriptions/effects are unmounted normally. No data request runs on module evaluation or merely from `register`. Only mounted, connected page observers request data. No private polling loop or `ctx.socket` is needed.

Allocate a fresh opaque registration generation per `register`, include it in query keys, and mark that registration disposed before retiring its requests. On disposal cancel its query prefix, remove only that generation's cache rows, and prevent completion publication; never clear the app's entire QueryClient. React Query cancellation alone cannot abort `ctx.rest`: its documented options have no `signal`. Late results must still be rejected by the §4 guards. Re-enable/remount must never adopt a disposed generation's cached payload.

Explicit desktop disable leaves **zero contributions, mounted page observers, plugin timers or subscriptions**. The app may still inventory the disabled plugin. Never write `host.pluginDecisions` or auto-enable anything. The Python backend's separate toggle is not changed by the desktop toggle.

## 3. Frozen data interface and query plan

### 3.1 Requests and envelopes

Use only namespace-relative GETs via `ctx.rest(path, { method: 'GET', timeoutMs: 15000 })`:

| Resource | Relative path |
| --- | --- |
| subscriptions | `/subscriptions?view=<machine|profile>` |
| usage | `/usage?window=<7|30|90>&view=<machine|profile>` plus `&provider=<encoded-id>` only when selected |
| health | `/health?view=<machine|profile>` |

Never pass `/api/plugins/usage-deck` into `ctx.rest`, an absolute origin, a connection token, or a fabricated `connectionId` REST option. The host selects the active connection/profile. Omit `profile=`: the page follows the active gateway profile, not the focused chat tile or Capabilities' independent selector. There is no desktop-local profile enumeration or management override.

Keep the original envelope until validation: success `{ ok: true, data, meta }`, `meta.schema_version === 1`; failure `{ ok: false, error, meta }`. Data shapes remain:

- subscriptions: `{ cards, inventory_state: 'detected'|'empty', view: 'machine'|'profile' }`.
- usage: `{ window_days, provider_id, series, reason }`.
- health: `{ entries }`.

Card, meter, holder, series/point, health, metadata and error fields/enums are exactly the existing DTOs in `models/dto.py` and `DESIGN.md` §§2–3; do not invent translation DTOs on the server. Reject missing/invalid resource arrays, unsupported schema, invalid effective view and wrong usage window as malformed responses, not empty inventory. Preserve `null`, explicit zero, `stale`, timestamps, source, scope, period and reason. Never display raw transport messages/HTML or private diagnostics; translate known error/reason codes to fixed local text and render server labels/DTO JSON as React text only.

### 3.2 Query configuration

Use the app's SDK `useQuery` and shared `queryClient`, one independent observer per resource. Exact key layout:

`['usage-deck', generation, epoch, connectionKey, profileKey, requestedView, resource, windowKey, providerKey]`

`resource` is `subscriptions`, `usage` or `health`; `windowKey` is selected days for usage and `null` otherwise; `providerKey` is selected id for usage and `''` otherwise. `epoch` comes from the reactive activation hook; `connectionKey` is the connection atom value or the literal `ambient` when null; `profileKey` is the active profile atom's exact string. `ambient` is not a host address and is safe only together with the epoch. Never key by profile name or socket state alone.

All three use `refetchInterval: 60000`, `refetchIntervalInBackground: false`, `staleTime: 0`, `retry: false`, `refetchOnWindowFocus: true`. Enable only when the generation is live, gateway is `open`, required identity exports exist and the captured scope still matches current scope. Do not use previous-key placeholder data, cross-key `initialData`, persistent query caches or `keepPreviousData`. Page unmount stops polling. Manual Refresh refetches the three current keys, honoring the same connected/scope guard; explain that backend snapshots may be cached for 30 seconds (`service.py:49,215–228`). No cache-busting parameter exists.

Parallel resource loading/failure is intentional. One failed zone must not blank two successful zones. A same-key background refresh can retain previous data only with a visible refreshing/stale indicator; §4 overrides this on disconnect or scope change.

### 3.3 View and filter semantics

Default requested view is `machine`, window `30`, provider `''` (All). Controls offer Machine/Profile and 7/30/90 days. View affects subscriptions and health; usage remains session analytics for the selected active profile even when machine inventory is selected (`service.py:243–270`). Show `All (this profile)` and `This Hermes profile only — not subscription allowance` beside analytics.

Effective inventory view is **only** `subscriptions.data.view`. Native/profile-restricted principals force profile view regardless of `view=machine` (`plugin_api.py:64–67`, `service.py:177–190`); an inventory source without machine enumeration also falls back (`service.py:134–138`). Reflect the requested control separately from the returned scope: if Machine was requested but Profile returned, show `Profile view — this connection does not expose machine inventory`. Never relabel those cards or health empties as machine-wide. Until subscriptions succeeds, label health scope `Requested machine/profile view; effective scope not yet confirmed`; do not assert machine coverage from health (it has no view field).

Provider options are unique ids/labels from current-scope subscription cards. Reset selection to All on scope change, and if the selected provider disappears after successful inventory. Do not carry an old provider into a new host while options load. A selected provider's analytics can legitimately return `series: []`, `reason: 'attribution_unavailable'`; explain it, never copy the profile total into that account. View/window/provider state is memory-only; no persistence of DTOs or identities.

## 4. Active gateway isolation and remote behavior

### 4.1 Routing boundary

For the user's PC → `192.168.0.150` connection, `ctx.rest('/subscriptions…')` means **that active gateway's** `/api/plugins/usage-deck/subscriptions`, not the PC's local gateway. SDK context delegates to `pluginRest`, which applies active profile scoping and calls `hermesApi`; that wrapper attaches the active registry connection id for Electron routing (source citations in §1). The app resolves URL, transport and authentication. UsageDeck never hardcodes this IP, opens its own HTTP connection, forwards credentials, or falls back to localhost. A null connection id can be an ambient/legacy local or remote source: null does not prove there is no gateway.

Read `host.state.gateway`, `host.state.profile`, and (when available) `host.state.connectionId` reactively with `useValue`. Use `ui.useGatewayActivationEpoch()` in render and `ui.gatewayActivationEpoch()` in asynchronous guards. The epoch is a transient invalidation token, not a durable host identity. It changes even on same-profile source reselection, where socket/profile values alone might not produce a render. Do not substitute a plain epoch getter in render, `host.status()`, profile-route guessing, or a timer.

### 4.2 Capture, fetch, and publish rule

The page's scope tuple is `(generation, epoch, connectionKey, profileKey)`. A scope-keyed child owns all resource observers and local provider controls. On every new tuple, render that child's fresh loading state immediately; never wait for an effect to remove the old host's cards. The page render must also check live scope/current gateway before exposing any cached DTO.

For each queryFn, capture the tuple; immediately before `ctx.rest` compare it with live getters and require gateway `open` and non-disposed registration. After both successful resolution **and rejection**, compare again. If mismatched, throw an internal scope-retired cancellation sentinel; never return old data or expose the old error text as a new host's error. Queued retries/refetch callbacks must perform the pre-call check, not blindly use a captured old key against the new active REST transport.

Cancel/remove retired scope queries by this plugin's exact prefix when switching; removal can occur during lifecycle cleanup but must not be the only protection. The tuple-keyed render and post-await guard are mandatory even if cancellation or removal loses a race. This covers A → B, A → B → A, same profile name on two hosts, switch while a response or error is pending, and switches while the page is unmounted. New tuples always refetch; revisiting a host never resurrects an older activation's cache.

### 4.3 No gateway / disconnect

When gateway is not `open`, disable requests and render `Connect to a gateway to view UsageDeck` (or `Connecting to gateway…` for `connecting`). Hide all old cards, analytics, JSON dialogs, endpoint health and old errors, even if QueryClient retains rows. Disable Refresh; keep the sidebar route so the explanation is discoverable. On reconnect refetch the current scope. Socket openness is only readiness, never host identity.

### 4.4 Missing/disabled backend and other failures

A gateway without this agent half, an unmounted route, or backend disabled can reject REST. Show a bounded zone error such as `UsageDeck backend unavailable on the active gateway. Install/enable its agent half there, then refresh.` for identifiable missing-route/501 failures. Do not assert that every 404 proves an absent package: `provider_not_found` is a valid UsageDeck failure and invalidates the provider filter. Authentication/403 failure says to reconnect/check gateway access, never asks for provider credentials. 400 means invalid query; 503 means source unavailable; transport/timeout and malformed payload have their own local errors. If the SDK provides no reliable status/body, use the generic active-gateway backend-unavailable error rather than regex-guessing arbitrary private text.

Valid empty inventory is a successful response, visually distinct from unreachable backend. Failure in one resource keeps other current-scope zones visible. Same-scope last data may be shown stale on an ordinary transient refresh failure; access-denied, missing backend and disconnect hide prior payloads. No provider-login form and no automatic backend installation/enable action.

### 4.5 Desktop compatibility guard

Use namespace feature detection before mounting the data page. Minimum supported identity surface: readable `host.state.connectionId`, `host.state.profile`, `host.state.gateway`, `gatewayActivationEpoch` and `useGatewayActivationEpoch`. If missing, contribute the same route/nav but show `Update Hermes Desktop to use connection-safe UsageDeck`; issue **no REST requests** and show no cached DTOs. Never invent an SDK hook in production or weaken isolation to support an older PC build. QA must test this branch and rollout must check the user's actual desktop build. The hook/export is verified in the pinned supplemental source (§1); it is not promised by every older desktop release.

## 5. Presentation and numerical honesty

### 5.1 Layout and controls

One scrollable, responsive page in this order: header (`UsageDeck`, read-only explanation, active profile/connection label using credential-free id, last successful update and Refresh); `Subscriptions` grid with view control; `Usage analytics` with window/provider controls; `Self-hosted health` compact strip. Grid collapses to one column when narrow; charts/labels must not overflow. Use semantic headings/sections and accessible labels. Each zone has independent skeleton, empty, ready, stale and error treatment; errors have explicit user-triggered retry.

Use kit `ui.Button`, `ui.Badge`, `ui.Skeleton`, `ui.EmptyState`/`ui.ErrorState`, `ui.Select*` or `ui.SegmentedControl`, `ui.Dialog*`, `ui.ScrollArea`, `ui.StatusDot` as appropriate with their actual supported props. Semantic HTML/SVG for layout/chart/progress is allowed; `ui.Card`/`ui.Progress` must not be invented. Theme variables only; no dashboard CSS classes assumed, fixed color palette, DOM scraping, global styles or global listeners. Keyboard users can reach all controls; status is textual, not color-only; badges expose source/scope/time in tooltip or accessible description.

### 5.2 Subscription cards

Each card shows provider/account label, plan or `Plan unknown`, `unverified claim` when applicable, credential state, card confidence, holder profile/root labels in effective machine view, stale/checked time, and meters or an honest reason. Preserve credential taxonomy: `available` means `Credential present (not validated)`, `missing` means `No credential found`, `rejected` means `Credential rejected`. Presence is never successful validation. Error cards are isolated; opaque/unavailable cards never gain estimated quota from analytics. Explain supported-source absence with fixed local copy (Codex workspace spend-control availability, Anthropic capture availability, and machine-only inventory distinctions), consistent with baseline dashboard `dist/index.js:61–103,379–399` and `DESIGN.md` §§12–13.

Meters show used/limit/remaining where reported, unit, period, observed/reset times, and confidence. Negative remaining is `Overspent` with positive magnitude. Anthropic header utilization shows used percent/reset, not invented remaining; wham spend-control shows reported credits and reset, not USD/messages/fleet totals. No unsupported new rate-window source mapping is introduced under the frozen baseline. `Show JSON` opens a kit modal containing only the sanitized card DTO as text, with Escape, focus management and trigger-focus restoration; close it on scope change/disconnect.

### 5.3 Confidence and progress

Match baseline dashboard's confidence rules (`dist/index.js:37–59,133–209`): labels Exact/Estimated/Opaque/Self-hosted; source ceilings `openrouter_key=exact`, `anthropic_headers=exact`, `chatgpt_wham_usage=exact`, `session_analytics=estimated`, `prometheus=self_hosted`, `none/unknown=opaque`. Clamp exact/estimated claims to their ceiling; incompatible self-hosted/cloud categories collapse to opaque. Error/opaque cards have opaque badges; a ready card without a measured supported meter is opaque. Card badge cannot exceed the best measured meter or its own declared confidence. An empty supported-telemetry card can retain its declared cloud confidence; self-hosted requires a measured self-hosted meter.

Unknown/null/nonfinite values render `Not reported`/`—`, never zero. Opaque meters render no numbers or bars. Zero remains a real reading. Progress requires non-opaque measured value, finite value ≥ 0, finite limit > 0, and nonempty period label other than `unknown`. Show true percentage even above 100; clamp visual fill to 100 and label over-limit. Do not divide by zero or imply a known reset period. Every measured number's confidence must be visible in its meter/series/health context.

### 5.4 Analytics and health

Render profile series as separate token and estimated-cost charts, with source/confidence/scope and accessible summaries/legend. Native SVG is sufficient. Sort valid day points and split lines at null values **and missing days**; do not connect unknown gaps, backfill zero, combine USD with credits/tokens, or imply subscription allowance. Empty series explain `no_observations` or `attribution_unavailable`. Tokens/cost remain backend session analytics, not inferred provider billing.

Health rows show display name, textual Healthy/Degraded/Offline/Unknown, finite latency, CPU/memory only if present, confidence, metrics availability, checked time, stale and reason/error. `metrics_available: false` cannot support a Self-hosted badge; downgrade that claim to Opaque. Unknown unprobed inventory is not offline; render `Not probed` for source-unavailable machine inventory. Empty strip means no endpoints in the confirmed scope, not a healthy machine.

## 6. Install, update, uninstall, and version stamp

### 6.1 Local unified-package path

Repository path `plugins/usage_deck/` is the package subdirectory, not a new installation id. Install that **package folder** as `usage-deck` in an app-machine local Hermes `plugins/` root (default home or a profile). Existing `plugin.yaml`, dashboard manifest/backend and staging conventions remain intact; do not relocate the backend to match a documentation example. Electron main discovers `desktop/plugin.js`, stages/copies its desktop half into app-local `$HERMES_HOME/desktop-plugins/usage-deck/`, writes its own `.hermes-package.json` origin marker, and renames atomically. Renderer scans only that app-local copy (snapshot lines 2130–2172). The developer/plugin must not ship or fabricate the marker.

A package-root `plugin.js` would take precedence; do not add one. The copy is app-level across profiles and gateways, not installed once per active remote profile. A marker-less standalone destination with a `plugin.js` is not overwritten; resolve such a conflict explicitly in Plugins UI rather than overwriting user files. Source tree symlinks/oversize content can prevent copying; the documented linked-package development mode is distinct from symlinks *inside* a copied desktop payload.

### 6.2 Remote topology and fallback

Installing the agent package only on `192.168.0.150` **does not sync its desktop files to the PC**. The remote filesystem is not scanned. Snapshot lines 2208–2216 explicitly say a remote-only install shows Desktop `unavailable (remote backend)`, not a pending copy. When installing through the desktop remote install dialog, the app separately clones the Desktop target into the PC's desktop-plugins root; this is a local install action, not automatic remote-file replication.

Fallback procedure for the user/rollout operator:

1. On the PC, open Capabilities → Plugins → Install from Git. Select the repository/package containing `plugins/usage_deck` (same package subdirectory as the agent install), keep the **Desktop** target checked, and confirm the normal install review. Do not create a second agent install on the PC unless intended. Verify that the installer actually identifies the nested package/desktop half before confirming.
2. If that desktop build cannot select the nested package, obtain the same repository revision locally on the PC and install the `plugins/usage_deck` package folder through the supported local package/file install path. Rescan. This uses the local unified-package mechanism of §6.1; it does not require PC provider credentials or enabling a PC agent half. If the build offers neither path, update it; do not claim a remote rescan transfers bytes.
3. Confirm app-local `desktop-plugins/usage-deck/plugin.js` is present and matches the intended revision; enable the Desktop half explicitly in Plugins. Separately ensure the agent half is installed/enabled on the active remote gateway under its own consent rules. Reconnect/Refresh and verify the displayed profile/effective view.

The Git URL/package-subdirectory syntax must be taken from the target installer, not invented here. A manually copied standalone `desktop-plugins/usage-deck/plugin.js` can be a diagnostic last resort, but lacks unified provenance/reconcile/uninstall behavior and is not the shipped installation acceptance path.

### 6.3 Update and development reload

Installed package copies refresh on Rescan, app install/update or restart when source `plugin.js` is newer; editing the installed source alone need not update the running copy. For development, link a local checkout package into the PC's local `plugins/usage-deck` root using the platform-supported link mechanism. The app marks that copy `linked: true`, watches source changes by content and re-syncs/hot-reloads. Standalone copied desktop files are watched directly. Command-palette `Reload desktop plugins` reloads the current copy; it cannot repair stale source-to-copy sync. `hermes plugins doctor <path-or-id>` diagnoses stale copies (snapshot lines 2174–2198). Reload must retire the old generation (§2.2), not double the nav or query observers.

### 6.4 Removal and version

Removing the local source package causes reconciliation to remove its marked desktop copy; Rescan/restart if necessary. Removing only a remote agent package does not remove an independently cloned PC desktop half: remove that Desktop target locally too (or leave it disabled with backend-unavailable state). A standalone marker-less copy must be removed through its own local plugin management path; do not promise remote uninstall cleans it. Desktop disable alone is reversible and neither deletes nor disables Python.

Version remains **0.1.0**, matching both `plugins/usage_deck/plugin.yaml:2` and manifest `:6` (and the staged dashboard manifest). No metadata bump is required or allowed by this desktop-only builder card. The frozen `HermesPlugin` interface has no documented version field: use a `// UsageDeck desktop 0.1.0` comment stamp in `plugin.js`, not a fictional host API field or new desktop manifest. Release identity/evidence additionally records the exact git SHA/hash, since version alone cannot distinguish revisions. Future package version bumps must update canonical/staged metadata through a separately scoped release change.

## 7. Headless QA contract and evidence ownership

### 7.1 Deliverables and executable interface

Verifier owns `tests/desktop/harness.mjs` and `tests/desktop/contract.test.mjs` (repository-level tests convention). Keep harness/support code out of the one-file shipped desktop directory. No Python file edits, backend test seams, app modifications, or dist imports. If the QA card cannot publish these files, report that to dispatch for a QA artifact/PR; do not silently widen the builder's constrained PR.

Required entry point from repo root:

`node --experimental-vm-modules --test tests/desktop/contract.test.mjs`

The suite has deterministic mock-backend scenarios and an integration mode selected by `USAGE_DECK_DESKTOP_BASE_URL` naming an explicitly supplied loopback test-host origin; auth comes from a private environment/file reference, never a committed credential or command-line token. Integration-required runs must fail/exit nonzero if routes/auth are unavailable, not silently skip them. Record separate scenario and real-route verdicts. The verifier may stage this harness as a QA attachment until dispatch integrates its test-only change.

### 7.2 Loader and rendering proof

Read the **actual PR-head plugin.js** and load it as ESM, e.g. through Node `vm.SourceTextModule` with an explicit linker allow-list. Reject every specifier except §1's three. Link `react`/`react/jsx-runtime` to the same real React instance; use a real React renderer plus a real React Query client/provider for mounted tests. SDK mocks expose only names independently verified in the snapshot/pinned source, including real semantics for atoms, query observers, reactive epoch and supported kit props. Kit components may be transparent accessible test adapters, but React/hooks/query scheduling must not be replaced with a fake function that returns canned data. Resolve test dependencies from the installed desktop dependency tree or an isolated pinned test-tool directory and record versions; do not add production dependencies/build files to this package.

Mock ctx must capture registrations, scope source/id behavior, disposers and `rest` calls. Mock `host.state` is reactive, with gateway/profile/connection atoms and a separately reactive epoch; state changes must actually trigger renders. Load default export, call `register`, mount the **registered page's render output**, await query settlement, traverse rendered text/props/semantic DOM, and operate the actual controls. Source-string assertions alone or pure helper tests are not acceptance.

### 7.3 Real-route bridge on this host

Integration ctx.rest forwards namespace-relative paths to the real Hermes-host-mounted `/api/plugins/usage-deck/...` via a test-side authenticated HTTP bridge. Read status/body and reject non-2xx in the same shape verified for the target SDK; record that shape. Do not bypass via `UsageDeckService`, direct fixture JSON, or a bare hand-mounted FastAPI router while calling it host integration.

Prefer an isolated scratch Hermes home and an ephemeral dashboard on this machine, following the existing `tests/acceptance/host_acceptance.py` isolation/consent/session-seeding approach and `tests/acceptance/hermes_python.py` interpreter resolution. Existing helpers can run unchanged; JS harness orchestration may launch the real host and attach to its routes. The host's synthetic provider endpoints/network guard keep upstream traffic inside existing acceptance policy; no real user credential/config mutation and no new external destinations. A deliberate read-only live-host invocation is supplementary and must identify that target without exporting auth/private DTOs. QA reports capture route status/schema/resource counts and renderer assertions, not raw private responses.

For each real resource, compare independently parsed route DTO fields to actual rendered payload (card identity/state/unit/confidence, usage window/observations, health status). A successful empty host proves route/render emptiness only: use controlled synthetic real-host data for nonempty coverage. Exercise both machine-owner requests and profile-restricted principal behavior; for the latter the real route must clamp machine to profile and the renderer must label it honestly. Do not pretend a mock principal proves production authorization. If the available host cannot mint/test a native principal, record that integration sub-gate as unverified and retain it for rollout rather than issuing an unqualified PASS.

### 7.4 Required scenario matrix

Each row must have behavioral assertions, not just screenshots:

| Gate | Minimum assertion |
| --- | --- |
| Module/contributions | Valid ESM/default contract; exact two route/nav payloads; opt-in; allowed imports only; no import-time REST; no Python/dist/metadata diff |
| Happy payload | Nonempty subscription, token/cost series and health responses reach registered page; each zone can succeed independently |
| Cadence | Real query options/controlled clock prove 60000 ms foreground interval, no 59999 ms poll, stopped observer on unmount, manual Refresh current scope only |
| Scope isolation | A and B use same profile name but distinct sentinel DTOs; deferred A success **and failure** after switch never appear under B; A→B→A gets fresh epoch; same connection/profile epoch-only activation rerenders and refetches; queued old refetch cannot call new transport under old key |
| Offline/missing | No gateway makes zero REST calls and hides prior payloads; reconnect loads fresh; 404/501 unavailable vs valid empty; 401/403 access; 503/timeout/malformed; scope change does not retain old error/dialog |
| View/filter | Machine default, returned profile clamp copy, unconfirmed health scope, 7/30/90 paths, encoded provider, vanished provider reset, profile analytics never labeled machine allowance |
| Honesty | Exact/estimated/opaque/self-hosted ceilings; null vs zero, unknown source/period, limit zero, over-limit and negative remaining; credits not USD; missing vs rejected credential; unavailable meter not numeric zero; no invented provider attribution |
| Charts/health | Null and missing-day gaps do not connect; empty/unattributed text; metrics-unavailable confidence downgrade; unprobed not offline; per-zone stale/error isolation |
| Safety/accessibility | Hostile labels render as text; DTO JSON modal, Escape/focus behavior, labeled controls and badge source/scope/time; no raw transport private text |
| Lifecycle | Simulated host disabled decision never invokes register; disable removes both contributions and observers; pending result ignored; re-enable/hot reload contributes exactly once with a new generation; older SDK branch makes zero REST calls |

### 7.5 Proof boundary and rollout

Headless PASS proves actual module loading against the documented surface, mounted resource rendering, query/lifecycle/isolation behavior, and (only when executed) real host route/auth/schema integration. Mock registry/REST cannot prove Electron's copier, IPC routing, native grant transport, real kit visuals or the PC's installed SDK compatibility. Existing backend tests alone prove none of this desktop behavior. Record exact PR SHA, frozen backend SHA/diff check, node/dependency versions, commands/exit status, scenario counts, route mode and remaining gates.

Rollout owns a one-time real-PC check: local Desktop target and provenance/copy revision; both explicit enables; visible nav/page/native kit; actual active remote `192.168.0.150` data/profile scope; switch to a second gateway or disconnect with no old-host flash; disable/re-enable; Rescan/update and removal behavior. User eyeball verifies layout/readability, modal focus and theme/narrow sizing. Never report production desktop deployment or remote-file synchronization from the headless mock harness alone.

## 8. Completion gates by lane

- **Design:** this numbered document is committed on repository main; no implementation/Python changes. Supplemental identity exports are grounded and older-build behavior is explicit.
- **Builder:** a PR containing only §1's source file, mapping §2–6 requirements to implementation, with no backend/metadata/egress drift. Scope deviations require dispatch adjudication, not silent backend fixes.
- **Verifier:** publish §7's runnable harness plus exact-head execution evidence and explicit real-route/native-principal limits. Missing acceptance execution is not PASS.
- **Review:** independently check implementation against these sections and verify evidence at integrated exact head; do not approve the author's own design spike as implementation.
- **Rollout:** install/update on the actual PC and active gateway through normal consent, complete §7.5's real-app gate, and distinguish installed/enabled/visually accepted from merely merged code.
