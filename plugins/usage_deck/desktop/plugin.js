// UsageDeck desktop 0.1.0
//
// Desktop half of the unified UsageDeck package (DESIGN-desktop.md §§1-6).
// Plain uncompiled ESM, no JSX syntax, no build step. The only imports are the
// three specifiers the disk-plugin loader resolves: @hermes/plugin-sdk, react,
// react/jsx-runtime. All data comes from the existing backend routes through
// ctx.rest (namespace-relative GETs); the host picks the active gateway,
// profile and credentials. Nothing here touches dashboard/dist or any backend.
//
// Honesty rules carried over from the dashboard tab (DESIGN.md §4-5):
//  - null/unknown is never rendered as zero; opaque data never gets a number or bar;
//  - a confidence badge never exceeds what the meter's source allows;
//  - every server string is rendered as React text only;
//  - cached rows are keyed to the active gateway activation, so a host switch can
//    never show another host's data, and a disconnect hides everything.

import * as ui from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PLUGIN_ID = 'usage-deck'
const ROUTE_PATH = '/usage-deck'
const QUERY_ROOT = 'usage-deck'
const POLL_MS = 60000
const REST_TIMEOUT_MS = 15000
const WINDOWS = [7, 30, 90]
const DEFAULT_WINDOW = 30
const VIEWS = ['machine', 'profile']
const DEFAULT_VIEW = 'machine'
const ALL_PROVIDERS = '__usage_deck_all__'
// Usage analytics dimension (analytics-dimensions-breakdown §7): how scoped session records are partitioned.
const DIMENSIONS = ['all', 'profile', 'model', 'repo']
const DEFAULT_DIMENSION = 'all'
const USAGE_SCHEMA_VERSION = 2
const COPY_SCOPE_MACHINE = 'All local profile session records — not subscription allowance.'
const COPY_SCOPE_PROFILE = 'This Hermes profile only — not subscription allowance.'
const COPY_ATTRIBUTION_LIMIT =
  "Attribution follows each session's recorded model and provider (its last recorded values after a switch), counted once per session; it does not reconstruct every model call."
const ATTRIBUTION_TIP = {
  session_provider: 'Provider recorded on the session.',
  billing_provider: "Provider taken from the session's billing provider; not measured billing ownership.",
  models_json: "Provider inferred from this profile's local models.json catalog by exact model id; not measured billing ownership.",
  unknown: 'No provider was recorded for this session and the local catalog does not name one.'
}
const USAGE_REASONS = { no_observations: true, source_unavailable: true }
const METRIC_FIELDS = ['input_count', 'output_count', 'estimated_usd', 'sessions', 'api_calls']
const GROUP_PALETTE = 8

const CONF_LABEL = { exact: 'Exact', estimated: 'Estimated', opaque: 'Opaque', self_hosted: 'Self-hosted' }
const CONF_RANK = { opaque: 0, estimated: 1, exact: 2 }
// Highest confidence a given data source can honestly carry (DESIGN-desktop §5.3).
const SOURCE_CEILING = {
  openrouter_key: 'exact',
  session_analytics: 'estimated',
  prometheus: 'self_hosted',
  anthropic_headers: 'exact',
  anthropic_oauth_usage: 'exact',
  chatgpt_wham_usage: 'exact',
  none: 'opaque'
}
const SOURCE_LABEL = {
  openrouter_key: 'OpenRouter key endpoint',
  session_analytics: 'Hermes session analytics',
  prometheus: 'Endpoint metrics',
  anthropic_headers: 'Anthropic unified rate-limit headers captured from the last call',
  anthropic_oauth_usage: 'Anthropic OAuth usage endpoint (read live)',
  chatgpt_wham_usage: 'ChatGPT workspace spend control (wham/usage)',
  none: 'No verified source'
}
const SCOPE_LABEL = {
  api_credential: 'API credential',
  provider_account: 'Provider account',
  endpoint: 'Endpoint',
  profile: 'This Hermes profile'
}
const UNIT_LABEL = { usd: 'USD', count: 'count', percent: 'percent', bytes: 'bytes', credit: 'credits' }

// Fixed local copy for provider-specific opaque cards (baseline dashboard dist/index.js:61-70).
const OPAQUE_NOTES = {
  'openai-codex':
    'No Codex spend meter for this account: UsageDeck reads only the ChatGPT workspace spend control, and this account has none it can show right now (no usable credential, an unrecognised or expired reading, or access was not permitted). Nothing is estimated. Any plan shown is an unverified claim from the credential.',
  anthropic:
    'No Anthropic session/weekly meter: no live usage reading for this credential (the live read needs a persisted OAuth login of the selected profile) and no usable capture; enable the optional observer and use this account normally. A captured meter can only appear after an ordinary request returns eligible headers (DESIGN.md §12.1). Nothing is estimated.'
}
// Anthropic card whose live usage endpoint answered but yielded no meter (DESIGN.md §9.1.2): the live answer is
// definitive, so no older captured reading is shown in its place.
const ANTHROPIC_LIVE_EMPTY_NOTE =
  "No Anthropic session/weekly meter: Anthropic's usage endpoint was read live for this credential and reported no current session or weekly window (or did not permit the read). Nothing is estimated and no older captured reading is shown in its place."
const MACHINE_ONLY_NOTE =
  "No live source for this account: the machine view reads meters with any local profile's credential only through the documented read-only usage probes (Codex spend control, Anthropic OAuth usage, endpoint /metrics and models listing), and none exists for this account, so it is local inventory. Nothing is estimated."
// Machine view, self-hosted health row of an endpoint owned by ANOTHER profile's inventory (config or credential pool; ownership, not proof of a request) (DESIGN.md §13 item 3b).
const PROFILE_OWNED_BADGE = 'Profile endpoint'
const PROFILE_OWNED_NOTE =
  "This endpoint belongs to another profile's inventory (its config or its credential pool), not to the selected profile. The machine view's read-only probe (/metrics and the models listing) uses that profile's own address, anonymously unless that profile's own key_env supplies a key; when that key is unset, no request is sent. No other profile's or the process's credential is sent. The row's status shows the outcome."

// Fixed local copy for Reason codes: upstream text never reaches the UI.
const REASON_TEXT = {
  unsupported: 'No verified usage source exists for this provider.',
  credentials_missing: 'No credential was sent for this account (none was found).',
  permission_denied: 'The available credential does not permit reading this data.',
  attribution_unavailable: 'Provider attribution unavailable from session analytics.',
  no_observations: 'No observations for this period.',
  metrics_unavailable: 'This endpoint does not expose supported metrics.',
  source_unavailable: 'The data source is currently unavailable.',
  box_offline: 'Box offline: the endpoint is not reachable right now.'
}
// "Last seen" text of a self-hosted card or health entry (server-process memory; null = never seen since start).
function lastSeenText(obj) {
  if (!isObj(obj)) return null
  const seen = str(obj.last_seen_at)
  return seen ? 'Last seen ' + fmtTime(seen) : 'Not seen since the server started'
}
// Fixed local copy for backend error codes (models/dto.py ERROR_CODES).
const DECK_ERROR_TEXT = {
  invalid_query: 'The request parameters were not valid.',
  provider_not_found: 'The selected provider is not configured in this profile.',
  auth_required: 'The provider rejected the stored credential.',
  permission_denied: 'The credential does not permit reading usage.',
  timeout: 'The provider did not answer in time.',
  upstream_unavailable: 'The provider could not be reached.',
  malformed_response: 'The provider returned data in an unexpected format.',
  inventory_unavailable: 'The provider inventory could not be read on the active gateway.',
  analytics_unavailable: 'Session analytics are unavailable.',
  internal_error: 'An internal error occurred.'
}
const AUTH_INFO = {
  available: {
    text: 'Credential present (not validated)',
    tip: 'A credential was found and is used for requests to this provider. It has not been confirmed to work.'
  },
  missing: {
    text: 'No credential found',
    tip: 'No credential was found, so none was sent. A provider that answers 401 to an unauthenticated request does not mean the credential is wrong.'
  },
  rejected: {
    text: 'Credential rejected',
    tip: 'A credential was sent and the provider answered 401 (unauthorized). The stored credential is probably wrong or expired.'
  }
}
const HEALTH_META = {
  healthy: { icon: '●', text: 'Healthy', tone: 'good' },
  degraded: { icon: '▲', text: 'Degraded', tone: 'warn' },
  offline: { icon: '✕', text: 'Offline', tone: 'bad' },
  unknown: { icon: '?', text: 'Unknown', tone: 'muted' }
}
const BADGE_VARIANT = { exact: 'success', estimated: 'warn', opaque: 'muted', self_hosted: 'default' }

const NOTICE_UPDATE = 'Update Hermes Desktop to use connection-safe UsageDeck'
const NOTICE_CONNECT = 'Connect to a gateway to view UsageDeck'
const NOTICE_CONNECTING = 'Connecting to gateway…'
const BACKEND_UNAVAILABLE =
  'UsageDeck backend unavailable on the active gateway. Install/enable its agent half there, then refresh.'

// Theme variables only (SDK "Style with theme variables, never hardcoded colors").
const TEXT_2 = 'var(--ui-text-secondary)'
const TEXT_3 = 'var(--ui-text-tertiary)'
const STROKE = 'var(--ui-stroke-secondary)'
const ACCENT = 'var(--ui-accent)'
const SURFACE = 'var(--ui-bg-quaternary)'
const SURFACE_2 = 'var(--ui-bg-tertiary)'

const SMALL = { fontSize: '0.75rem', lineHeight: '1.1rem' }
const MUTED = { ...SMALL, color: TEXT_3 }
const SR_ONLY = {
  position: 'absolute',
  width: '1px',
  height: '1px',
  padding: 0,
  margin: '-1px',
  overflow: 'hidden',
  clip: 'rect(0, 0, 0, 0)',
  whiteSpace: 'nowrap',
  border: 0
}

// ---------------------------------------------------------------------------
// Tiny element helper over jsx()/jsxs() (no JSX syntax, no createElement shim)
// ---------------------------------------------------------------------------

function h(type, props, ...kids) {
  const { key, ...rest } = props || {}
  const children = kids.filter(k => k !== undefined && k !== null && k !== false)

  if (children.length === 0) {
    return jsx(type, rest, key)
  }

  if (children.length === 1) {
    return jsx(type, { ...rest, children: children[0] }, key)
  }

  return jsxs(type, { ...rest, children }, key)
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

function isObj(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

function num(v) {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function str(v) {
  return typeof v === 'string' ? v : null
}

function has(map, key) {
  return typeof key === 'string' && Object.prototype.hasOwnProperty.call(map, key)
}

function asConfidence(v) {
  return has(CONF_LABEL, v) ? v : 'opaque'
}

// Lower of a claimed confidence and the ceiling its source allows. exact/estimated are
// ordered tiers; self_hosted is a source category, so any cross-category mismatch
// collapses to opaque rather than guessing.
function effectiveConfidence(own, ceiling) {
  const a = asConfidence(own)
  const c = asConfidence(ceiling)

  if (a === c) {
    return a
  }

  if (a === 'opaque' || c === 'opaque') {
    return 'opaque'
  }

  if (a === 'self_hosted' || c === 'self_hosted') {
    return 'opaque'
  }

  return CONF_RANK[a] <= CONF_RANK[c] ? a : c
}

function meterConfidence(m) {
  const ceiling = has(SOURCE_CEILING, m && m.source) ? SOURCE_CEILING[m.source] : 'opaque'

  return effectiveConfidence(m && m.confidence, ceiling)
}

// A meter carries at least one finite number and is not opaque.
function isMeasured(m) {
  if (!isObj(m) || meterConfidence(m) === 'opaque') {
    return false
  }

  return num(m.value) !== null || num(m.limit) !== null || num(m.remaining) !== null
}

// Progress needs non-opaque measured value, finite value >= 0, finite limit > 0 and a
// known period label (§5.3). Returns the raw (uncapped) ratio or null.
function progressRatio(m) {
  if (!isMeasured(m)) {
    return null
  }

  const value = num(m.value)
  const limit = num(m.limit)

  if (value === null || limit === null || value < 0 || limit <= 0) {
    return null
  }

  const label = isObj(m.period) ? str(m.period.label) : null

  if (!label || label === 'unknown') {
    return null
  }

  return value / limit
}

// Card-level badge: never above what the card's measured meters (or its own claim) support.
function cardConfidence(card) {
  const own = asConfidence(card && card.confidence)

  if (!card || card.state === 'opaque' || card.state === 'error') {
    return 'opaque'
  }

  const measured = (Array.isArray(card.meters) ? card.meters : []).filter(isMeasured)

  if (card.state === 'ready' && measured.length === 0) {
    return 'opaque'
  }

  if (own === 'opaque') {
    return 'opaque'
  }

  if (own === 'self_hosted') {
    return measured.some(m => meterConfidence(m) === 'self_hosted') ? 'self_hosted' : 'opaque'
  }

  if (measured.length === 0) {
    return own // empty state: supported telemetry, no observations
  }

  let best = 'opaque'

  measured.forEach(m => {
    const c = meterConfidence(m)

    if (has(CONF_RANK, c) && CONF_RANK[c] > CONF_RANK[best]) {
      best = c
    }
  })

  return CONF_RANK[own] <= CONF_RANK[best] ? own : best
}

// What the card body shows: error | meters | empty | unavailable. A "ready" card with no
// measured value is shown as unavailable, never as a zero.
function cardView(card) {
  if (!isObj(card)) {
    return 'unavailable'
  }

  if (card.state === 'error' || (card.state !== 'ready' && card.state !== 'opaque' && card.state !== 'empty')) {
    return 'error'
  }

  const measured = (Array.isArray(card.meters) ? card.meters : []).filter(isMeasured)

  if (measured.length > 0) {
    return 'meters'
  }

  return card.state === 'empty' ? 'empty' : 'unavailable'
}

function fmtNumber(n, unit) {
  const v = num(n)

  if (v === null) {
    return '—'
  }

  if (unit === 'usd') {
    const abs = Math.abs(v)

    return (
      (v < 0 ? '-' : '') +
      '$' +
      abs.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: abs >= 1 ? 2 : 4 })
    )
  }

  if (unit === 'bytes') {
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
    let x = Math.abs(v)
    let i = 0

    while (x >= 1024 && i < units.length - 1) {
      x /= 1024
      i++
    }

    return (v < 0 ? '-' : '') + (i === 0 ? String(Math.round(x)) : x.toFixed(1)) + ' ' + units[i]
  }

  if (unit === 'percent') {
    return Math.round(v * 10) / 10 + '%'
  }

  if (unit === 'credit') {
    return v.toLocaleString('en-US', { maximumFractionDigits: 2 }) + ' credits'
  }

  return v.toLocaleString('en-US', { maximumFractionDigits: 2 })
}

// "2026-11-06T12:30:00Z" -> "2026-11-06 12:30 UTC" (deterministic, unambiguous).
function fmtTime(iso) {
  const s = str(iso)

  if (!s) {
    return 'unknown time'
  }

  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(s)

  return m ? m[1] + ' ' + m[2] + ' UTC' : 'unknown time'
}

function fmtEpochMs(ms) {
  return fmtTime(new Date(ms).toISOString().replace(/\.\d+Z$/, 'Z'))
}

function dayToMs(day) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(typeof day === 'string' ? day : '')

  if (!m) {
    return null
  }

  const t = Date.UTC(+m[1], +m[2] - 1, +m[3])

  return Number.isFinite(t) ? t : null
}

// Split a day-ascending series into contiguous runs for one field. A run breaks at a null
// value AND at a missing calendar day, so a chart never bridges unknown gaps or plots
// unobserved days as zero (§5.4).
function splitSegments(points, field) {
  const segments = []
  let cur = null
  let prevT = null

  ;(Array.isArray(points) ? points : []).forEach(p => {
    const t = dayToMs(p && p.day)
    const v = p ? num(p[field]) : null

    if (t === null || v === null) {
      cur = null
      prevT = null

      return
    }

    if (cur && prevT !== null && t - prevT === 86400000) {
      cur.push({ day: p.day, t, v })
    } else {
      cur = [{ day: p.day, t, v }]
      segments.push(cur)
    }

    prevT = t
  })

  return segments
}

function niceMax(max) {
  if (!(max > 0)) {
    return 1
  }

  const mag = Math.pow(10, Math.floor(Math.log10(max)))
  const steps = [1, 2, 2.5, 5, 10]

  for (let i = 0; i < steps.length; i++) {
    if (max <= steps[i] * mag) {
      return steps[i] * mag
    }
  }

  return 10 * mag
}

function sortedByDay(points) {
  return (Array.isArray(points) ? points : [])
    .filter(p => isObj(p) && dayToMs(p.day) !== null)
    .sort((a, b) => dayToMs(a.day) - dayToMs(b.day))
}

// Holder profile names of a machine-view card, in server order.
function holderNames(card) {
  const list = card && Array.isArray(card.holders) ? card.holders : []

  return list
    .filter(isObj)
    .map(holder => {
      const name = str(holder.profile)

      return name ? (holder.is_global === true ? name + ' (root)' : name) : null
    })
    .filter(Boolean)
}

// Why a machine-view card has no live source (held only by other profiles), or null.
function machineOnlyNote(card) {
  if (!isObj(card) || cardView(card) !== 'unavailable' || card.reason !== 'source_unavailable') {
    return null
  }

  if (
    card.provider_id === 'anthropic' &&
    isObj(card.debug) &&
    (card.debug.source === 'anthropic_headers' || card.debug.source === 'anthropic_oauth_usage')
  ) {
    return null // had a source
  }

  return holderNames(card).length > 0 ? MACHINE_ONLY_NOTE : null
}

// Explanation for an opaque card of a provider that could expose usage, or null.
function opaqueNote(card) {
  if (!isObj(card) || card.kind === 'self_hosted' || cardView(card) !== 'unavailable') {
    return null
  }

  if (card.provider_id === 'anthropic' && isObj(card.debug) && card.debug.source === 'anthropic_oauth_usage') {
    return ANTHROPIC_LIVE_EMPTY_NOTE
  }

  return has(OPAQUE_NOTES, card.provider_id) ? OPAQUE_NOTES[card.provider_id] : null
}

function authInfo(status) {
  const key = has(AUTH_INFO, status) ? status : 'missing'

  return { key, text: AUTH_INFO[key].text, tip: AUTH_INFO[key].tip }
}

function deckErrorText(code) {
  return has(DECK_ERROR_TEXT, code) ? DECK_ERROR_TEXT[code] : 'The request could not be completed.'
}

// ---------------------------------------------------------------------------
// Errors (§3.1 validation, §4.4 failure taxonomy). Everything user-visible is
// fixed local text; raw transport messages and server messages are never shown.
// ---------------------------------------------------------------------------

class UdError extends Error {
  constructor(kind, extra) {
    super(kind)
    this.name = 'UdError'
    this.kind = kind // access | backend_unavailable | invalid_query | source_unavailable | timeout | transport | malformed | deck | retired
    this.code = (extra && extra.code) || null
    this.status = (extra && extra.status) || null
  }
}

function describeError(err) {
  const e = err instanceof UdError ? err : new UdError('backend_unavailable')

  switch (e.kind) {
    case 'access':
      return {
        title: 'Gateway access error',
        message:
          'The active gateway denied this request. Reconnect or check your access to this gateway and profile. UsageDeck never asks for provider credentials.'
      }
    case 'backend_unavailable':
      return { title: 'UsageDeck unavailable', message: BACKEND_UNAVAILABLE }
    case 'invalid_query':
      return { title: 'Invalid request', message: 'The active gateway rejected the request parameters.' }
    case 'source_unavailable':
      return {
        title: 'Source unavailable',
        message: 'The UsageDeck data source on the active gateway is unavailable right now.'
      }
    case 'timeout':
      return { title: 'Timed out', message: 'The active gateway did not answer in time.' }
    case 'malformed':
      return {
        title: 'Unexpected response',
        message: 'The active gateway returned UsageDeck data in an unexpected format.'
      }
    case 'deck':
      return {
        title: 'UsageDeck error',
        message: deckErrorText(e.code),
        detail: 'Code: ' + e.code + (e.status ? ' · HTTP ' + e.status : '')
      }
    case 'retired':
      return { title: 'Reloading', message: 'The gateway scope changed; reloading.' }
    default:
      return {
        title: 'Request failed',
        message: 'The UsageDeck request failed' + (e.status ? ' (HTTP ' + e.status + ').' : '.')
      }
  }
}

// Errors that must hide any previously shown payload (§4.4).
const HIDES_DATA = { access: true, backend_unavailable: true }

function knownDeckCode(obj) {
  if (!isObj(obj)) {
    return null
  }

  let e = obj.error

  if (!isObj(e) && isObj(obj.detail)) {
    e = isObj(obj.detail.error) ? obj.detail.error : obj.detail
  }

  return isObj(e) && has(DECK_ERROR_TEXT, e.code) ? e.code : null
}

// The REST door rejects with "<status>: <body>" (api-transport httpStatusError) and may
// carry a structured statusCode; use only those two shapes. No other free text is
// interpreted.
function readRestFailure(e) {
  let status = null
  let body = null

  if (e && typeof e === 'object') {
    const structured = Number(e.statusCode ?? e.status)

    if (Number.isInteger(structured) && structured >= 100 && structured <= 599) {
      status = structured
    }

    if (isObj(e.body)) {
      body = e.body
    }

    const raw = typeof e.message === 'string' ? e.message : ''
    const m = /^(?:Error invoking remote method '[^']*': )?(?:Error: )?(\d{3}): ([\s\S]*)$/.exec(raw)

    if (m) {
      if (status === null) {
        status = parseInt(m[1], 10)
      }

      if (body === null) {
        try {
          const parsed = JSON.parse(m[2])

          body = isObj(parsed) ? parsed : null
        } catch {
          body = null
        }
      }
    }
  }

  return { status, body }
}

function classifyRestError(e) {
  if (e instanceof UdError) {
    return e
  }

  const { status, body } = readRestFailure(e)
  const code = knownDeckCode(body)

  if (status === 401 || status === 403) {
    return new UdError('access', { status })
  }

  if (code) {
    return new UdError('deck', { code, status })
  }

  if (status === 501 || status === 404 || status === 405) {
    return new UdError('backend_unavailable', { status })
  }

  if (status === 400) {
    return new UdError('invalid_query', { status })
  }

  if (status === 503) {
    return new UdError('source_unavailable', { status })
  }

  if (e && typeof e === 'object' && (e.name === 'TimeoutError' || e.code === 'ETIMEDOUT')) {
    return new UdError('timeout', { status })
  }

  if (status !== null) {
    return new UdError('transport', { status })
  }

  // No reliable status/body from the SDK: use the generic active-gateway error (§4.4).
  return new UdError('backend_unavailable')
}

// `version` is the wire version the endpoint speaks: 1 for subscriptions/health, 2 for usage.
function unwrapEnvelope(resp, version) {
  if (!isObj(resp)) {
    throw new UdError('malformed')
  }

  if (resp.ok === false) {
    const code = knownDeckCode(resp)

    throw code ? new UdError('deck', { code }) : new UdError('malformed')
  }

  if (resp.ok !== true || !isObj(resp.meta) || resp.meta.schema_version !== (version || 1) || !isObj(resp.data)) {
    throw new UdError('malformed')
  }

  return { data: resp.data, meta: resp.meta }
}

function validateSubscriptions(env) {
  const d = env.data

  if (!Array.isArray(d.cards) || !d.cards.every(isObj)) {
    throw new UdError('malformed')
  }

  if (d.view !== 'machine' && d.view !== 'profile') {
    throw new UdError('malformed')
  }

  if (d.inventory_state !== 'detected' && d.inventory_state !== 'empty') {
    throw new UdError('malformed')
  }

  return { cards: d.cards, inventory_state: d.inventory_state, view: d.view, meta: env.meta }
}

function validMetrics(m) {
  return isObj(m) && Array.isArray(m.missing_fields) && METRIC_FIELDS.every(f => m[f] === null || num(m[f]) !== null)
}

// Validate the v2 usage payload (analytics-dimensions-breakdown §4). `query` is {provider, mode, dimension} as
// requested: the answer must be for the same window and provider filter (a stale or unfiltered answer is never
// shown under a filtered label). mode/dimension come from the response: the server may clamp them
// (profile-restricted principal) and the UI renders the EFFECTIVE scope.
function validateUsage(env, windowDays, query) {
  const d = env.data
  const q = query || {}
  const bad = () => new UdError('malformed')

  if (d.window_days !== windowDays || (d.provider_id ?? null) !== (q.provider || null)) {
    throw bad()
  }

  if ((d.mode !== 'machine' && d.mode !== 'profile') || !DIMENSIONS.includes(d.dimension)) {
    throw bad()
  }

  if (q.mode && q.dimension && d.mode === q.mode && d.dimension !== q.dimension) {
    throw bad()
  }

  for (const k of ['series', 'groups', 'rows', 'providers', 'sources']) {
    if (!Array.isArray(d[k]) || !d[k].every(isObj)) {
      throw bad()
    }
  }

  if (!validMetrics(d.totals)) {
    throw bad()
  }

  if (!d.groups.every(g => typeof g.id === 'string' && typeof g.series_id === 'string' && Array.isArray(g.row_ids) && validMetrics(g.totals))) {
    throw bad()
  }

  if (!d.rows.every(r => typeof r.id === 'string' && validMetrics(r.metrics))) {
    throw bad()
  }

  return {
    window_days: d.window_days,
    provider_id: str(d.provider_id),
    mode: d.mode,
    dimension: d.dimension,
    series: d.series,
    groups: d.groups,
    rows: d.rows,
    totals: d.totals,
    providers: d.providers.filter(p => str(p.id) && str(p.label)).map(p => ({ id: p.id, label: p.label })),
    sources: d.sources,
    complete: d.complete !== false,
    top_n: num(d.top_n),
    reason: has(USAGE_REASONS, d.reason) ? d.reason : null,
    pricing: isObj(d.pricing) ? d.pricing : null,
    depth: decodeDepth(d),
    meta: env.meta
  }
}

// Dimension options of a mode: machine offers All | By profile | By model | By repo, a single profile All | By model | By repo.
function dimensionOptions(mode) {
  const all = [
    { id: 'all', label: 'All' },
    { id: 'profile', label: 'By profile' },
    { id: 'model', label: 'By model' },
    { id: 'repo', label: 'By repo' }
  ]

  return mode === 'profile' ? all.filter(o => o.id !== 'profile') : all
}

// A by-profile view does not exist for a single profile.
function effectiveDimension(mode, dimension) {
  return mode === 'profile' && dimension === 'profile' ? 'all' : dimension
}

// Total tokens of a metrics object: null when neither component is known; `partial` when only one is.
function totalTokens(m) {
  const i = num(m && m.input_count)
  const o = num(m && m.output_count)

  if (i === null && o === null) {
    return { value: null, partial: false }
  }

  return { value: (i || 0) + (o || 0), partial: i === null || o === null }
}

// Cell text of a possibly unavailable count: null is a dash, a measured zero is 0.
function fmtMetric(v, unit) {
  return num(v) === null ? '—' : fmtNumber(v, unit || 'count')
}

// <api-equivalent-helpers>
// API-equivalent cost (docs/design/api-equivalent-pricing.md §7). Pure helpers, no rendering: the SAME block (byte
// for byte) lives in dist/index.js and desktop/plugin.js (tests/frontend/api_equivalent.test.js enforces it). The
// frontend never fetches prices or resolves models: it only formats the backend's integer pico-USD strings.
const API_LABEL = "API-equivalent ($)";
const API_TIP = "Hypothetical cost at current base text API rates; not actual spend. Excludes cache discounts, context tiers and extra fees.";
const API_FOOTNOTE = "API-equivalent ($) is what the recorded input and output tokens would cost at current public base text API rates. It is a hypothetical figure, not actual spend, an invoice or a subscription cost, and it excludes cache discounts, context tiers, batch discounts and tool or media fees. A model with no known price shows \u2014 and is left out of every sum; a price proxy is a stand-in tariff, not a verified identity.";
const API_REASON_TEXT = {
  tokens_missing: "token counts not recorded",
  tokens_invalid: "token counts invalid",
  model_unknown: "no usable model id",
  no_catalog_match: "no price known for this model id",
  ambiguous_model: "several different prices match this model id",
  alias_target_missing: "the price entry its alias points to is missing",
};
const API_WARNING_TEXT = {
  pricing_stale: "Stale prices: some catalog prices are more than 7 days old. Amounts are kept, not blanked.",
  pricing_refresh_failed: "Price refresh failed; showing the last known prices.",
  pricing_cache_invalid: "The local price cache was unreadable; showing the packaged prices.",
  pricing_cache_unwritable: "The price cache could not be saved; prices refresh in memory only.",
  pricing_aliases_invalid: "The alias table was invalid; only catalog matches can price a model.",
  pricing_seed_invalid: "No usable price catalog is available; every model is unpriced.",
};
const API_PICO_RE = /^(?:0|[1-9][0-9]{0,30})$/;
const API_PICO_PER_USD = BigInt("1000000000000");
const API_PICO_PER_CENT = BigInt("10000000000");

/** Exact integer pico-USD string of a metrics object, or null (null is "no priced observation", never zero). */
function apiPico(m) {
  const a = m && m.api_equivalent;
  const s = a && typeof a === "object" && !Array.isArray(a) ? a.amount_pico_usd : null;
  return typeof s === "string" && API_PICO_RE.test(s) ? s : null;
}

/** Exact decimal USD text of a pico-USD integer string ("1234567" -> "0.000001234567"). */
function picoToUsd(s) {
  const v = BigInt(s);
  const frac = (v % API_PICO_PER_USD).toString().padStart(12, "0").replace(/0+$/, "");
  return (v / API_PICO_PER_USD).toString() + (frac ? "." + frac : "");
}

/** Screen label: null -> dash, exact zero -> $0.00, nonzero under one cent -> <$0.01, else two decimals. */
function fmtApi(pico) {
  if (pico === null) return "\u2014";
  const v = BigInt(pico);
  if (v === BigInt(0)) return "$0.00";
  if (v < API_PICO_PER_CENT) return "<$0.01";
  const cents = (v + API_PICO_PER_CENT / BigInt(2)) / API_PICO_PER_CENT;
  const whole = (cents / BigInt(100)).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return "$" + whole + "." + (cents % BigInt(100)).toString().padStart(2, "0");
}

function apiCount(v) {
  return typeof v === "number" && isFinite(v) && v >= 0 ? Math.floor(v) : 0;
}

/** Programmatic view of one metrics object: state is "null" | "zero" | "value" (never conflated). */
function apiView(m) {
  const raw = m && m.api_equivalent;
  const a = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const pico = apiPico(m);
  return {
    state: pico === null ? "null" : pico === "0" ? "zero" : "value",
    pico: pico,
    text: fmtApi(pico),
    exact: pico === null ? null : "$" + picoToUsd(pico),
    priced: apiCount(a.priced_rows),
    unpriced: apiCount(a.unpriced_rows),
    proxy: apiCount(a.proxy_rows),
    partial: a.partial === true,
    stale: a.stale === true,
  };
}

/** Visible qualifiers: partial subtotal, unpriced count, price proxy, stale prices. */
function apiFlags(v) {
  const out = [];
  if (v.partial && v.pico !== null) out.push({ id: "partial", text: "Partial \u00b7 " + v.unpriced + " unpriced" });
  else if (v.pico === null && v.unpriced > 0) out.push({ id: "unpriced", text: v.unpriced + " unpriced" });
  if (v.proxy > 0) out.push({ id: "proxy", text: v.priced > 1 ? "Price proxy \u00b7 " + v.proxy + " of " + v.priced : "Price proxy" });
  if (v.stale) out.push({ id: "stale", text: "Stale prices" });
  return out;
}

/** Hover/tooltip text: a row explains its resolved key and method; an aggregate lists its row counts. */
function apiTitle(v, row, rowCount) {
  const lines = [];
  const p = row && row.pricing && typeof row.pricing === "object" ? row.pricing : null;
  if (row) lines.push("Model: " + (typeof row.model_id === "string" ? row.model_id : "Unknown model"));
  if (p && typeof p.catalog_key === "string") {
    lines.push("Priced as " + p.catalog_key + " (" + (typeof p.method === "string" ? p.method : "unknown method") +
      (typeof p.alias_kind === "string" ? ", " + p.alias_kind : "") + ")");
    if (typeof p.note === "string" && p.note) lines.push(p.note);
    if (typeof p.fetched_at === "string") lines.push("Prices fetched " + p.fetched_at + (p.stale === true ? " \u2014 stale (older than 7 days)" : ""));
  }
  if (v.pico === null) {
    const why = p && typeof p.reason === "string" && Object.prototype.hasOwnProperty.call(API_REASON_TEXT, p.reason) ? API_REASON_TEXT[p.reason] : "no priced observation";
    lines.push("Not priced: " + why + ". Excluded from every API-equivalent sum; not zero.");
  } else {
    lines.push("Exact: " + v.exact + " (" + v.pico + " pico-USD)");
  }
  if (!row) {
    lines.push(v.priced + " priced, " + v.unpriced + " unpriced" + (v.proxy ? ", " + v.proxy + " on a price proxy" : "") +
      (typeof rowCount === "number" ? " of " + rowCount + " daily rows" : "") + ".");
  }
  lines.push(API_TIP);
  return lines.join("\n");
}

/** The explicit unpriced bucket: rows in data.pricing.unknown_row_ids, grouped by exact model id and reason. */
function unpricedModels(data) {
  const rows = data && Array.isArray(data.rows) ? data.rows : [];
  const ids = data && data.pricing && Array.isArray(data.pricing.unknown_row_ids) ? data.pricing.unknown_row_ids : null;
  const byId = {};
  rows.forEach(function (r) { if (r && typeof r.id === "string") byId[r.id] = r; });
  // No data.pricing block (older backend) means "not reported", never "unpriced": no bucket is invented.
  const list = ids ? ids.map(function (id) { return byId[id]; }).filter(Boolean) : [];
  const groups = {};
  list.forEach(function (r) {
    const model = typeof r.model_id === "string" ? r.model_id : null;
    const code = r.pricing && typeof r.pricing.reason === "string" ? r.pricing.reason : null;
    const key = JSON.stringify([model, code]);
    if (!groups[key]) groups[key] = { model: model === null ? "Unknown model" : model, code: code, reason: code && Object.prototype.hasOwnProperty.call(API_REASON_TEXT, code) ? API_REASON_TEXT[code] : "not priced", rows: 0 };
    groups[key].rows += 1;
  });
  return Object.keys(groups).map(function (k) { return groups[k]; }).sort(function (a, b) { return a.model < b.model ? -1 : a.model > b.model ? 1 : 0; });
}

/** Human text for the response's pricing warning codes (known vocabulary only). */
function pricingNotes(data) {
  const w = data && data.pricing && Array.isArray(data.pricing.warnings) ? data.pricing.warnings : [];
  return w.filter(function (c) { return Object.prototype.hasOwnProperty.call(API_WARNING_TEXT, c); }).map(function (c) { return { code: c, text: API_WARNING_TEXT[c] }; });
}
// </api-equivalent-helpers>

// <depth-helpers>
// Analytics depth (docs/design/analytics-depth-breakdown.md §4-§7). Pure helpers, no rendering: the SAME block
// (byte for byte) lives in dist/index.js and desktop/plugin.js (tests/frontend/depth.test.js enforces it).
// null means "not recorded" everywhere: it is never coerced to 0, never averaged and never drawn as a zero cell.
const DEPTH_FIELDS = ["durations", "duration_samples", "heatmap", "cache_metrics", "repo_rollup", "model_rollup", "depth_totals", "depth_status"];
const DEPTH_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const DURATION_LABEL = "Completed session duration";
const CACHE_LABEL = "Effective recorded $/Mtok (weighted diagnostic)";
const CACHE_TIP = "Recorded cost divided by base-cache-weighted recorded counters. Reasoning may overlap output; not a tariff, invoice, or measured savings.";
const CACHE_NOTE = "Recorded-cost basis: base-cache weighting of recorded counters. No prices are fetched and nothing here is a bill.";
const HEAT_NOTE = "Each session is counted once, in the hour it started (never split across midnight or hours).";
const CACHE_REASON_TEXT = {
  missing_usage: "Some sessions did not record every token counter.",
  missing_cost: "Some sessions did not record a cost.",
  unknown_discount: "No published cache discount for this exact provider and model.",
  zero_denominator: "No weighted tokens to divide by.",
  incomplete_source: "Some session stores could not be read; ratios are not computed over a partial set.",
};
const DEPTH_MISSING_TEXT = {
  input_tokens: "input tokens", output_tokens: "output tokens", estimated_cost_usd: "cost", cache_read_tokens: "cache reads",
  cache_write_tokens: "cache writes", reasoning_tokens: "reasoning tokens", duration: "a completed duration",
};

function depthCount(v) {
  return typeof v === "number" && isFinite(v) && v >= 0 && Math.floor(v) === v ? v : null;
}

function depthNum(v) {
  return typeof v === "number" && isFinite(v) && v >= 0 ? v : null;
}

function depthText(v) {
  return typeof v === "string" ? v : null;
}

function depthGroup(n) {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function depthPad(n) {
  return n < 10 ? "0" + n : String(n);
}

/** Decode the eight additive members of a usage payload. An older server omits all of them: `supported` is false. */
function decodeDepth(d) {
  const own = function (o, k) { return Object.prototype.hasOwnProperty.call(o, k); };
  const supported = !!d && typeof d === "object" && DEPTH_FIELDS.every(function (k) { return own(d, k); });
  const out = { supported: supported, durations: null, samples: null, heatmap: null, cache: null, repos: null, totals: null, status: null };
  if (!supported) return out;
  const du = d.durations;
  if (du && typeof du === "object" && !Array.isArray(du)) {
    out.durations = { p50: depthNum(du.p50), p90: depthNum(du.p90), p99: depthNum(du.p99), mean: depthNum(du.mean), max: depthNum(du.max) };
    out.samples = depthCount(d.duration_samples);
  }
  const hm = d.heatmap;
  if (hm && typeof hm === "object" && !Array.isArray(hm)) {
    // `starts` (exact epochs) is what makes a LOCAL rendering correct; without a trustworthy list there is none.
    let starts = Array.isArray(hm.starts) ? hm.starts : null;
    if (starts) {
      starts = starts.map(function (s) {
        const ok = s && typeof s === "object" && typeof s.started_at === "number" && isFinite(s.started_at)
          && depthCount(s.sessions) !== null && s.sessions >= 1 && (s.total_tokens === null || depthCount(s.total_tokens) !== null)
          && !isNaN(new Date(s.started_at * 1000).getTime());
        return ok ? { t: s.started_at, n: s.sessions, tokens: s.total_tokens } : null;
      });
      if (starts.some(function (s) { return s === null; })) starts = null;
    }
    out.heatmap = { starts: starts };
  }
  out.cache = Array.isArray(d.cache_metrics) ? d.cache_metrics.filter(function (r) { return r && typeof r === "object"; }) : null;
  out.repos = Array.isArray(d.repo_rollup) ? d.repo_rollup.filter(function (r) { return r && typeof r === "object"; }) : null;
  const t = d.depth_totals;
  if (t && typeof t === "object") out.totals = { sessions: depthCount(t.sessions), tokens: depthCount(t.total_tokens), cost: depthNum(t.total_cost) };
  const s = d.depth_status;
  if (s && typeof s === "object") {
    const missing = {};
    const raw = s.missing && typeof s.missing === "object" ? s.missing : {};
    Object.keys(DEPTH_MISSING_TEXT).forEach(function (k) { missing[k] = depthCount(raw[k]); });
    out.status = { partial: s.partial === true, excluded: depthCount(s.excluded_started_at), missing: missing };
  }
  return out;
}

/** Human text for a count of seconds; null is a dash. The exact value is always available as a tooltip. */
function fmtDuration(sec) {
  const s = depthNum(sec);
  if (s === null) return "\u2014";
  if (s < 60) return String(Math.round(s * 10) / 10) + " s";
  const whole = Math.round(s);
  if (whole < 3600) return Math.floor(whole / 60) + "m " + (whole % 60) + "s";
  return Math.floor(whole / 3600) + "h " + Math.floor((whole % 3600) / 60) + "m";
}

function exactSeconds(sec) {
  const s = depthNum(sec);
  return s === null ? "Not recorded." : String(s) + " seconds";
}

/** The five stats and the sample count as display items (every null stays a dash). */
function durationItems(stats, samples) {
  const st = stats || {};
  const items = ["p50", "p90", "p99", "mean", "max"].map(function (k) {
    return { id: k, label: k, text: fmtDuration(st[k]), title: exactSeconds(st[k]) };
  });
  const n = depthCount(samples);
  items.push({ id: "samples", label: "samples", text: n === null ? "\u2014" : depthGroup(n), title: "Completed sessions with a valid start and end." });
  return items;
}

/** Browser-local weekday/hour cell of one epoch (seconds), judged by the zone rules of THAT instant. */
function localCellOf(epochSec) {
  const d = new Date(epochSec * 1000);
  return { row: (d.getDay() + 6) % 7, col: d.getHours() };
}

/** Re-bucket the exact start epochs into the 7x24 local grid (row-major, Monday first). Each start counts once. */
function localHeat(starts) {
  const cells = [];
  for (let i = 0; i < DEPTH_DAYS.length * 24; i++) cells.push({ sessions: 0, tokens: null });
  (starts || []).forEach(function (s) {
    const c = localCellOf(s.t);
    const cell = cells[c.row * 24 + c.col];
    cell.sessions += s.n;
    if (s.tokens !== null) cell.tokens = (cell.tokens || 0) + s.tokens;
  });
  return cells;
}

/** Colour level 0..4 and the "not recorded" flag of a cell for the active metric. */
function heatLevel(cell, metric, max) {
  if (metric === "tokens" && cell.sessions > 0 && cell.tokens === null) return { level: 0, na: true };
  const v = metric === "tokens" ? cell.tokens : cell.sessions;
  if (!v || !max) return { level: 0, na: false };
  return { level: Math.max(1, Math.min(4, Math.ceil((4 * v) / max))), na: false };
}

function heatMax(cells, metric) {
  let max = 0;
  cells.forEach(function (c) {
    const v = metric === "tokens" ? c.tokens : c.sessions;
    if (v !== null && v > max) max = v;
  });
  return max;
}

function heatCellLabel(row, col, cell, partial) {
  const tokens = cell.tokens === null ? (cell.sessions ? "tokens not recorded" : "no tokens") : depthGroup(cell.tokens) + " tokens";
  return DEPTH_DAYS[row] + " " + depthPad(col) + ":00, " + cell.sessions + (cell.sessions === 1 ? " session, " : " sessions, ") + tokens + (partial ? " (partial data)" : "");
}

function localOffset(epochSec) {
  const m = -new Date(epochSec * 1000).getTimezoneOffset();
  const a = Math.abs(m);
  return (m < 0 ? "-" : "+") + depthPad(Math.floor(a / 60)) + ":" + depthPad(a % 60);
}

/** The start cohorts inside one local cell (ISO instant + the local offset tell a repeated DST hour apart). */
function heatDetail(starts, row, col) {
  return (starts || []).filter(function (s) {
    const c = localCellOf(s.t);
    return c.row === row && c.col === col;
  }).sort(function (a, b) { return a.t - b.t; }).map(function (s) {
    return { t: s.t, iso: new Date(s.t * 1000).toISOString(), offset: localOffset(s.t), sessions: s.n, tokens: s.tokens };
  });
}

function localZoneName() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";
  } catch (e) {
    return "local time";
  }
}

function fmtPct(v) {
  const n = depthNum(v);
  return n === null ? "\u2014" : n.toFixed(2) + "%";
}

function fmtCacheCount(v) {
  const n = depthCount(v);
  return n === null ? "\u2014" : depthGroup(n);
}

function fmtCacheMoney(v) {
  const n = depthNum(v);
  return n === null ? "\u2014" : "$" + n.toFixed(4);
}

/** Provider labels of one per-model row: the backend lists every resolved provider comma-separated (ids never contain a comma). */
function providerList(value) {
  const text = depthText(value);
  return text === null ? [] : text.split(",").filter(function (t) { return t.length > 0; });
}

/** One cache-economics row for display. Reuses the backend numbers verbatim: no money is recomputed here. */
function cacheRowView(r) {
  const eff = depthNum(r.effective_cost_per_mtok);
  const mult = depthNum(r.discount_multiplier);
  const reason = depthText(r.reason);
  return {
    model: depthText(r.model) === null ? "Unknown model" : r.model,
    provider: depthText(r.provider_id),
    hit: fmtPct(r.hit_rate_pct),
    cached: fmtCacheCount(r.cached_input_tokens),
    uncached: fmtCacheCount(r.uncached_input_tokens),
    writes: fmtCacheCount(r.cache_write_tokens),
    reasoning: fmtCacheCount(r.reasoning_tokens),
    share: fmtPct(r.reasoning_share_pct),
    effective: fmtCacheMoney(eff),
    effectiveTitle: eff === null ? "Not computed." : "Exact: " + String(eff) + " USD per million weighted tokens (recorded-cost basis)",
    output: fmtCacheCount(r.output_tokens),
    multiplier: mult === null ? "\u2014" : String(mult),
    rateSource: depthText(r.rate_source) === null ? "\u2014" : r.rate_source,
    reason: reason,
    reasonText: reason !== null && Object.prototype.hasOwnProperty.call(CACHE_REASON_TEXT, reason) ? CACHE_REASON_TEXT[reason] : null,
  };
}

/** One-line disclosure of what was not recorded / readable (never replaces the legacy status cards). */
function depthStatusLine(status) {
  if (!status) return null;
  const bits = [];
  if (status.partial) bits.push("partial data");
  if (status.excluded) bits.push(status.excluded + " session" + (status.excluded === 1 ? "" : "s") + " without a valid start time left out");
  Object.keys(DEPTH_MISSING_TEXT).forEach(function (k) {
    const n = status.missing[k];
    if (n) bits.push(n + " without " + DEPTH_MISSING_TEXT[k]);
  });
  return bits.length ? "Known subtotals only: " + bits.join("; ") + "." : null;
}

/** The repo rollup row that belongs to a repo group (rollup rows carry no group id; label + shape identify them). */
function repoRowFor(group, rollup) {
  if (!group) return null;
  const label = depthText(group.label);
  const missing = group.id === "repo:null";
  const shaped = function (r) {
    return r.full_path === null && Array.isArray(r.full_paths) && r.full_paths.length === 0;
  };
  const named = (rollup || []).filter(function (r) { return r.repo_basename === label; });
  // "Unscoped" is both the missing bucket and a possible literal repository name: the empty-path row is the former.
  const hits = label === "Unscoped" && named.some(function (r) { return shaped(r) === missing; })
    ? named.filter(function (r) { return shaped(r) === missing; }) : named;
  if (hits.length <= 1) return hits[0] || null;
  const sessions = group.totals && group.totals.sessions;
  return hits.filter(function (r) { return r.sessions === sessions; })[0] || hits[0];
}

function repoPathSummary(r) {
  const paths = r && Array.isArray(r.full_paths) ? r.full_paths.filter(function (p) { return typeof p === "string"; }) : [];
  if (paths.length === 0) return { count: 0, text: null, paths: [] };
  return { count: paths.length, text: paths.length === 1 ? paths[0] : paths.length + " paths", paths: paths };
}

/**
 * Attribution coverage of the repository rollup: the share of KNOWN tokens that belong to a real repository (every
 * row except the missing/Unscoped one, which is the empty-path row labelled "Unscoped"). Null when no token count
 * is known. The percentage is floored to one decimal, so only a fully attributed rollup reads 100.0%.
 */
function repoCoverage(rollup) {
  if (!Array.isArray(rollup)) return null;
  let attributed = 0, unscoped = 0, known = false;
  rollup.forEach(function (r) {
    const n = r && typeof r === "object" ? depthCount(r.total_tokens) : null;
    if (n === null) return;
    known = true;
    const missing = r.repo_basename === "Unscoped" && r.full_path === null && Array.isArray(r.full_paths) && r.full_paths.length === 0;
    if (missing) unscoped += n; else attributed += n;
  });
  const total = attributed + unscoped;
  if (!known || total <= 0) return null;
  const pct = Math.floor(attributed * 1000 / total) / 10;
  return {
    attributed: attributed, unscoped: unscoped, total: total, pct: pct,
    text: pct.toFixed(1) + "% of tokens attributed to a repository (" + depthGroup(attributed) + " of " + depthGroup(total) +
      " known tokens; " + depthGroup(unscoped) + " Unscoped)",
  };
}
// </depth-helpers>

function scopeCopy(mode) {
  return mode === 'machine' ? COPY_SCOPE_MACHINE : COPY_SCOPE_PROFILE
}

function providerLabelOf(providers, id) {
  const hit = (providers || []).filter(p => p.id === id)[0]

  return hit ? hit.label : str(id) || 'Unknown'
}

function profileDbLabel(row) {
  return (row.is_global === true ? 'global (default home)' : str(row.profile) || 'profile') + ' · state.db'
}

// Groups joined with their series (by series_id) in server order; a group without a series is dropped.
function groupViews(data) {
  const byId = {}
  const rowsById = {}

  data.series.forEach(s => {
    if (str(s.id)) {
      byId[s.id] = s
    }
  })
  data.rows.forEach(r => {
    rowsById[r.id] = r
  })

  const out = []

  data.groups.forEach((g, i) => {
    const series = has(byId, g.series_id) ? byId[g.series_id] : null

    if (series) {
      out.push({ group: g, series, color: i % GROUP_PALETTE, rows: g.row_ids.map(id => rowsById[id]).filter(Boolean) })
    }
  })

  return out
}

// Theme-neutral categorical hue per group slot (the SDK has no categorical palette).
function groupStroke(i) {
  return 'hsl(' + ((i * 47 + 210) % 360) + ' 65% 55%)'
}

function validateHealth(env) {
  if (!Array.isArray(env.data.entries) || !env.data.entries.every(isObj)) {
    throw new UdError('malformed')
  }

  return { entries: env.data.entries, meta: env.meta }
}

// ---------------------------------------------------------------------------
// Registration generation + active-gateway identity (§2.2, §4)
// ---------------------------------------------------------------------------

let generationSeq = 0

function newGeneration() {
  generationSeq += 1

  return 'g' + generationSeq + '-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8)
}

function hostState() {
  return ui.host && ui.host.state ? ui.host.state : null
}

function isAtom(a) {
  return !!a && typeof a.get === 'function'
}

// §4.5: the connection-safe identity surface must exist or the page fails closed.
function identityReady() {
  const st = hostState()

  return (
    !!st &&
    isAtom(st.gateway) &&
    isAtom(st.profile) &&
    isAtom(st.connectionId) &&
    typeof ui.gatewayActivationEpoch === 'function' &&
    typeof ui.useGatewayActivationEpoch === 'function' &&
    typeof ui.useValue === 'function' &&
    typeof ui.useQuery === 'function' &&
    !!ui.queryClient
  )
}

function connectionKeyOf(v) {
  return typeof v === 'string' && v !== '' ? v : 'ambient'
}

function profileKeyOf(v) {
  return typeof v === 'string' ? v : ''
}

function readLive() {
  const st = hostState()

  return {
    gateway: st.gateway.get(),
    epoch: ui.gatewayActivationEpoch(),
    connectionKey: connectionKeyOf(st.connectionId.get()),
    profileKey: profileKeyOf(st.profile.get())
  }
}

function sameScope(a, b) {
  return a.epoch === b.epoch && a.connectionKey === b.connectionKey && a.profileKey === b.profileKey
}

// True only while this registration is live, the gateway is open and the live identity
// still equals the captured scope tuple.
function scopeIsLive(reg, scope) {
  if (reg.disposed || !identityReady()) {
    return false
  }

  const live = readLive()

  return live.gateway === 'open' && sameScope(live, scope)
}

function assertLive(reg, scope) {
  if (!scopeIsLive(reg, scope)) {
    throw new UdError('retired')
  }
}

// Remove this generation's cached rows that do not belong to `keep` (null = remove all).
// Removal is defence in depth: the scope-keyed render and the post-await guard are what
// guarantee isolation if cancellation loses a race.
function purge(reg, keep) {
  const qc = ui.queryClient

  if (!qc) {
    return
  }

  const predicate = q => {
    const k = q.queryKey

    return (
      Array.isArray(k) &&
      k[0] === QUERY_ROOT &&
      k[1] === reg.id &&
      !(keep && k[2] === keep.epoch && k[3] === keep.connectionKey && k[4] === keep.profileKey)
    )
  }

  try {
    const pending = qc.cancelQueries({ predicate })

    if (pending && typeof pending.catch === 'function') {
      pending.catch(() => {})
    }
  } catch {
    // cancellation is best effort
  }

  try {
    qc.removeQueries({ predicate })
  } catch {
    // removal is best effort
  }
}

// One guarded GET: capture-checked before the call and again after resolution AND
// rejection. A mismatch throws the retired sentinel; old data / old errors never surface.
async function guardedGet(reg, scope, path, validate, version) {
  assertLive(reg, scope)

  let raw
  let failure
  let failed = false

  try {
    raw = await reg.ctx.rest(path, { method: 'GET', timeoutMs: REST_TIMEOUT_MS })
  } catch (e) {
    failed = true
    failure = e
  }

  assertLive(reg, scope)

  if (failed) {
    throw classifyRestError(failure)
  }

  return validate(unwrapEnvelope(raw, version))
}

function useResourceQuery(reg, scope, spec) {
  const key = [
    QUERY_ROOT,
    scope.generation,
    scope.epoch,
    scope.connectionKey,
    scope.profileKey,
    spec.view,
    spec.resource,
    spec.windowKey,
    spec.providerKey,
    spec.dimension || ''
  ]

  return ui.useQuery({
    queryKey: key,
    queryFn: () => guardedGet(reg, scope, spec.path, spec.validate, spec.version),
    enabled: spec.enabled,
    refetchInterval: POLL_MS,
    refetchIntervalInBackground: false,
    staleTime: 0,
    retry: false,
    refetchOnWindowFocus: true,
    networkMode: 'always'
  })
}

// Collapse a query result into a render phase. `q` is null while the scope is not live.
function resourceState(q) {
  if (!q) {
    return { phase: 'loading', data: null, error: null, refreshing: false }
  }

  const err = q.error ? (q.error instanceof UdError ? q.error : new UdError('backend_unavailable')) : null

  if (err && err.kind === 'retired') {
    return { phase: 'loading', data: null, error: null, refreshing: false }
  }

  if (q.data === undefined) {
    return err
      ? { phase: 'error', data: null, error: err, refreshing: false }
      : { phase: 'loading', data: null, error: null, refreshing: false }
  }

  if (err && HIDES_DATA[err.kind]) {
    return { phase: 'error', data: null, error: err, refreshing: false }
  }

  return { phase: 'ready', data: q.data, error: err, refreshing: q.isFetching === true }
}

// ---------------------------------------------------------------------------
// Presentation building blocks
// ---------------------------------------------------------------------------

function LoadingBlock(props) {
  const rows = []

  for (let i = 0; i < (props.rows || 3); i++) {
    rows.push(h(ui.Skeleton, { key: i, 'aria-hidden': 'true', style: { height: '0.875rem', marginBottom: '0.5rem' } }))
  }

  return h('div', { role: 'status', 'aria-busy': 'true' }, h('span', { style: SR_ONLY }, props.label || 'Loading…'), rows)
}

function ConfBadge(props) {
  const conf = asConfidence(props.conf)
  const tip = props.tip || ''

  return h(
    ui.Badge,
    {
      variant: BADGE_VARIANT[conf],
      'data-confidence': conf,
      tabIndex: 0,
      title: tip || undefined,
      'aria-label': CONF_LABEL[conf] + ' confidence' + (tip ? '. ' + tip : '')
    },
    CONF_LABEL[conf]
  )
}

function StaleTag(props) {
  return h(
    ui.Badge,
    {
      variant: 'outline',
      title: props.refreshing
        ? 'Showing the last successful result while a newer request is pending.'
        : 'Showing the last successful result; a newer request failed.'
    },
    props.refreshing ? 'Refreshing' : 'Stale'
  )
}

function ErrorBlock(props) {
  const d = describeError(props.error)

  return h(
    'div',
    { role: 'alert', 'data-error-kind': props.error && props.error.kind, style: { padding: '0.5rem 0' } },
    h(
      ui.ErrorState,
      { title: d.title + (props.what ? ' · ' + props.what : ''), description: d.message },
      d.detail ? h('p', { style: { ...MUTED, textAlign: 'center' } }, d.detail) : null,
      props.onRetry
        ? h(
            'div',
            { style: { display: 'flex', justifyContent: 'center' } },
            h(ui.Button, { type: 'button', variant: 'outline', size: 'sm', onClick: props.onRetry }, 'Retry')
          )
        : null
    )
  )
}

function Zone(props) {
  const headingId = 'ud-h-' + props.id

  return h(
    'section',
    { 'aria-labelledby': headingId, 'data-zone': props.id, ...(props.dataAttrs || {}), style: { marginTop: '1.75rem' } },
    h(
      'div',
      {
        style: {
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          gap: '0.5rem 0.75rem',
          marginBottom: '0.5rem'
        }
      },
      h('h2', { id: headingId, style: { fontSize: '0.95rem', fontWeight: 600, margin: 0 } }, props.title),
      props.badge,
      h('div', { style: { marginLeft: 'auto', display: 'flex', flexWrap: 'wrap', gap: '0.5rem', alignItems: 'center' } }, props.controls)
    ),
    props.note ? h('p', { style: { ...MUTED, margin: '0 0 0.75rem' } }, props.note) : null,
    props.children
  )
}

function zoneBadge(rs) {
  return rs.phase === 'ready' && (rs.error || rs.refreshing) ? h(StaleTag, { refreshing: !rs.error && rs.refreshing }) : null
}

// ---------------------------------------------------------------------------
// Zone 1: subscription cards
// ---------------------------------------------------------------------------

function ValueRow(props) {
  return h(
    'div',
    null,
    h('dt', { style: MUTED }, props.label),
    h('dd', { style: { margin: 0, fontSize: '0.8rem' }, 'data-resets-at': props.resetsAt || undefined }, props.value)
  )
}

function MeterRow(props) {
  const m = props.meter
  const conf = meterConfidence(m)
  const unit = has(UNIT_LABEL, m.unit) ? m.unit : 'count'
  const ratio = progressRatio(m)
  const value = num(m.value)
  const limit = num(m.limit)
  const remaining = num(m.remaining)
  // Live pull (DESIGN.md §9.1.2): same used/resets/as-of rows as headers, but read at refresh time.
  const fromOauth = m.source === 'anthropic_oauth_usage'
  const fromHeaders = m.source === 'anthropic_headers' || fromOauth
  const fromSpend = m.source === 'chatgpt_wham_usage'
  const resetsAt = str(m.resets_at)
  const label = str(m.label) || 'Meter'
  const tip =
    'Source: ' +
    (SOURCE_LABEL[m.source] || SOURCE_LABEL.none) +
    '. Scope: ' +
    (SCOPE_LABEL[m.scope] || 'unknown') +
    '. Observed: ' +
    fmtTime(m.observed_at) +
    '.' +
    (resetsAt ? ' Reset reported by the provider: ' + fmtTime(resetsAt) + '.' : '') +
    (m.source === 'anthropic_headers' ? ' Value as of that call, not live; it is never extrapolated.' : '') +
    (fromOauth ? ' Read live from Anthropic when this view last refreshed; it is never extrapolated.' : '') +
    (fromSpend && unit === 'credit'
      ? " Credits reported for this seat's workspace spend limit, as of the last refresh; not USD, not messages, not a fleet total."
      : '')
  const notReported = v => (v === null ? 'Not reported' : fmtNumber(v, unit))
  const rows = []

  rows.push(h(ValueRow, { key: 'used', label: 'Used', value: notReported(value) }))

  if (!fromHeaders) {
    // Anthropic header utilization shows used percent + reset only: no invented limit/remaining.
    rows.push(h(ValueRow, { key: 'limit', label: 'Limit', value: notReported(limit) }))
    rows.push(
      h(ValueRow, {
        key: 'remaining',
        label: remaining !== null && remaining < 0 ? 'Overspent' : 'Remaining',
        value: remaining === null ? 'Not reported' : fmtNumber(remaining < 0 ? -remaining : remaining, unit)
      })
    )
  }

  if (fromHeaders || fromSpend) {
    rows.push(h(ValueRow, { key: 'resets', label: 'Resets', value: resetsAt ? fmtTime(resetsAt) : 'Not reported', resetsAt }))
  }

  rows.push(h(ValueRow, { key: 'asof', label: 'As of', value: fmtTime(m.observed_at) }))

  const parts = [
    h(
      'div',
      { key: 'head', style: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '0.5rem', justifyContent: 'space-between' } },
      h('span', { style: { fontSize: '0.8rem', fontWeight: 500 } }, label),
      h(ConfBadge, { conf, tip })
    ),
    h(
      'dl',
      { key: 'vals', style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(6.5rem, 1fr))', gap: '0.35rem 0.75rem', margin: '0.35rem 0 0' } },
      rows
    )
  ]

  if (ratio !== null) {
    const pct = Math.round(ratio * 1000) / 10

    parts.push(
      h(
        'div',
        {
          key: 'bar',
          role: 'progressbar',
          'aria-label': label + ' usage',
          'aria-valuemin': 0,
          'aria-valuemax': 100,
          'aria-valuenow': Math.min(100, pct),
          'aria-valuetext': pct + '% of limit' + (ratio > 1 ? ' (over limit)' : ''),
          style: { height: '0.4rem', borderRadius: '999px', background: SURFACE_2, overflow: 'hidden', marginTop: '0.5rem' }
        },
        h('div', { style: { height: '100%', width: Math.min(100, ratio * 100) + '%', background: ACCENT } })
      ),
      h('div', { key: 'pct', style: { ...SMALL, marginTop: '0.2rem' } }, ratio > 1 ? 'Over limit: ' + pct + '% of limit' : pct + '% of limit')
    )
  } else if (limit !== null && value !== null) {
    parts.push(
      h(
        'div',
        { key: 'nobar', style: { ...MUTED, marginTop: '0.35rem' } },
        "No progress bar: the limit's period is not known, so usage cannot be compared to it."
      )
    )
  }

  return h('li', { 'data-meter-id': str(m.id) || undefined, style: { listStyle: 'none', padding: '0.5rem 0', borderTop: '1px solid ' + STROKE } }, parts)
}

// "Show JSON": kit Dialog (Radix: Escape, focus trap, focus restored to the trigger).
// Content is the sanitized card DTO as text only. The card owns the open state, so the
// dialog unmounts with the card on scope change, disconnect or filter change.
function JsonDialog(props) {
  const [open, setOpen] = useState(false)
  const label = props.label

  return h(
    ui.Dialog,
    { open, onOpenChange: setOpen },
    h(ui.DialogTrigger, { asChild: true }, h(ui.Button, { type: 'button', variant: 'ghost', size: 'xs' }, 'Show JSON')),
    h(
      ui.DialogContent,
      null,
      h(
        ui.DialogHeader,
        null,
        h(ui.DialogTitle, null, 'Sanitized JSON · ' + label),
        h(ui.DialogDescription, null, 'The sanitized card data returned by the active gateway, shown as plain text.')
      ),
      h(
        'pre',
        {
          tabIndex: 0,
          'aria-label': 'Sanitized card JSON',
          style: { maxHeight: '60vh', overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: '0.7rem', margin: 0 }
        },
        props.text
      )
    )
  )
}

function SubscriptionCard(props) {
  const card = props.card
  const view = cardView(card)
  const conf = cardConfidence(card)
  const stale = card.stale === true || props.zoneStale
  const meters = (Array.isArray(card.meters) ? card.meters : []).filter(isMeasured)
  const plan = str(card.plan_name)
  const planText = plan ? 'Plan: ' + plan + (card.plan_source === 'unverified_claim' ? ' (unverified claim)' : '') : 'Plan unknown'
  const reasonText = has(REASON_TEXT, card.reason) ? REASON_TEXT[card.reason] : null
  const note = opaqueNote(card)
  const machineNote = machineOnlyNote(card)
  const holders = holderNames(card)
  const auth = authInfo(card.auth_status)
  const providerLabel = str(card.provider_label) || str(card.provider_id) || 'Provider'
  const accountLabel = str(card.account_label) || 'Account'
  let body

  if (view === 'error') {
    const code = isObj(card.error) && has(DECK_ERROR_TEXT, card.error.code) ? card.error.code : null

    body = h(ErrorBlock, {
      what: 'this account',
      error: code ? new UdError('deck', { code }) : new UdError('deck', { code: 'internal_error' }),
      onRetry: props.onRetry
    })
  } else if (view === 'meters') {
    body = h(
      'div',
      null,
      h('ul', { style: { margin: 0, padding: 0 } }, meters.map((m, i) => h(MeterRow, { key: str(m.id) || i, meter: m }))),
      card.state === 'opaque'
        ? h('p', { style: { ...MUTED, margin: '0.35rem 0 0' } }, 'Quota unavailable for this account.' + (reasonText ? ' ' + reasonText : ''))
        : null
    )
  } else if (view === 'empty') {
    body = h('p', { style: { ...SMALL, margin: 0 } }, 'No observations for this period.')
  } else {
    body = h(
      'div',
      { 'data-opaque': 'true' },
      h('p', { style: { fontSize: '0.8rem', fontWeight: 500, margin: '0 0 0.25rem' } }, 'Usage unavailable'),
      h('p', { style: { ...SMALL, margin: '0 0 0.25rem' } }, 'Data unavailable' + (reasonText ? ': ' + reasonText : ' from this source.')),
      card.reason === 'box_offline' ? h('p', { 'data-field': 'last-seen', style: { ...MUTED, margin: '0 0 0.25rem' } }, lastSeenText(card)) : null,
      h(
        'p',
        { style: { ...MUTED, margin: '0 0 0.25rem' } },
        card.kind === 'self_hosted'
          ? 'Endpoint health appears in the health strip below.'
          : "Session analytics only — profile-wide usage appears in the chart below, not as this account's allowance."
      ),
      note ? h('p', { style: { ...MUTED, margin: '0 0 0.25rem' }, 'data-opaque-note': str(card.provider_id) || undefined }, note) : null,
      machineNote ? h('p', { style: { ...MUTED, margin: 0 }, 'data-machine-only-note': 'true' }, machineNote) : null
    )
  }

  return h(
    'article',
    {
      'data-card-id': card.id,
      'data-view': view,
      'aria-busy': props.busy ? 'true' : undefined,
      'aria-label': providerLabel + ' ' + accountLabel,
      style: {
        border: '1px solid ' + STROKE,
        borderRadius: '6px',
        padding: '0.75rem',
        display: 'flex',
        flexDirection: 'column',
        gap: '0.5rem',
        minWidth: 0,
        opacity: stale ? 0.85 : 1
      }
    },
    h(
      'header',
      { style: { display: 'flex', justifyContent: 'space-between', gap: '0.5rem', alignItems: 'flex-start' } },
      h(
        'div',
        { style: { minWidth: 0 } },
        h(
          'h3',
          { style: { fontSize: '0.875rem', fontWeight: 600, margin: 0, overflowWrap: 'anywhere' } },
          providerLabel,
          plan ? h('span', { title: 'Plan claim read from the credential; not verified.', style: { fontWeight: 400, color: TEXT_2 } }, ' [' + plan + ']') : null
        ),
        h('div', { style: { ...MUTED, overflowWrap: 'anywhere' } }, accountLabel),
        holders.length
          ? h(
              'div',
              {
                style: { ...MUTED, overflowWrap: 'anywhere' },
                'data-holders': holders.join(','),
                title: 'Held by these Hermes homes (read from their auth.json; presence only, not validated).'
              },
              'Held by: ' + holders.join(', ')
            )
          : null
      ),
      h(
        'div',
        { style: { display: 'flex', flexWrap: 'wrap', gap: '0.25rem', justifyContent: 'flex-end' } },
        card.kind === 'self_hosted' ? h(ui.Badge, { variant: 'outline' }, 'Self-hosted endpoint') : null,
        h(ConfBadge, {
          conf,
          tip:
            'Primary source for this card. Checked: ' +
            fmtTime(card.checked_at) +
            '.' +
            (note && conf === 'opaque' ? ' ' + note : '') +
            (machineNote ? ' ' + machineNote : '')
        })
      )
    ),
    h(
      'div',
      { style: { ...SMALL, display: 'flex', flexWrap: 'wrap', gap: '0.25rem 0.5rem', alignItems: 'center', color: TEXT_2 } },
      h('span', null, planText),
      h('span', { 'aria-hidden': 'true' }, '·'),
      h('span', { 'data-auth-status': auth.key, title: auth.tip }, auth.text),
      stale ? h(StaleTag, { refreshing: false }) : null
    ),
    h('div', { style: { flex: 1 } }, body),
    h(
      'footer',
      { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '0.5rem' } },
      h('span', { style: MUTED }, 'Checked ' + fmtTime(card.checked_at)),
      h(JsonDialog, { label: providerLabel + ' ' + accountLabel, text: JSON.stringify(card, null, 2) })
    )
  )
}

function SubscriptionsZone(props) {
  const rs = props.rs
  const requested = props.view
  const data = rs.data
  const effective = data ? data.view : null
  let content

  if (rs.phase === 'loading') {
    content = h(
      'div',
      { style: gridStyle, 'aria-busy': 'true' },
      [0, 1, 2].map(i =>
        h('div', { key: i, style: { border: '1px solid ' + STROKE, borderRadius: '6px', padding: '0.75rem' } }, h(LoadingBlock, { rows: 4, label: 'Loading subscriptions…' }))
      )
    )
  } else if (rs.phase === 'error') {
    content = h(ErrorBlock, { what: 'subscriptions', error: rs.error, onRetry: props.onRetry })
  } else {
    const err = rs.error ? h(ErrorBlock, { key: 'err', what: 'subscriptions', error: rs.error, onRetry: props.onRetry }) : null
    const machine = effective === 'machine'

    if (data.cards.length === 0) {
      content = h(
        'div',
        null,
        err,
        h(
          'div',
          { 'data-state': 'first_run', style: { textAlign: 'center', padding: '1rem 0' } },
          h(ui.EmptyState, {
            title: machine ? 'No providers detected on this machine' : 'No providers detected in this profile',
            description:
              (machine
                ? "UsageDeck reads the existing credential pools of the global Hermes home and every profile on the active gateway's machine; it never asks for keys."
                : "UsageDeck reads this profile's existing Hermes configuration and credential pool on the active gateway; it never asks for keys.") +
              ' Add or configure a provider on that gateway and it will appear here.'
          }),
          h(ui.Button, { type: 'button', variant: 'outline', size: 'sm', onClick: props.onRetry }, 'Refresh')
        )
      )
    } else {
      content = h(
        'div',
        null,
        err,
        h(
          'div',
          { style: gridStyle, 'aria-busy': rs.refreshing ? 'true' : undefined },
          data.cards.map((c, i) =>
            h(SubscriptionCard, { key: str(c.id) || i, card: c, zoneStale: !!rs.error, busy: rs.refreshing, onRetry: props.onRetry })
          )
        )
      )
    }
  }

  let note
  let clamp = null

  if (effective === 'machine') {
    note =
      "Machine view: the global Hermes home plus every profile's credential pool on the active gateway's machine (auth.json only), merged only when a Codex login carries the same account and user identity. Each card lists the profiles that hold it; accounts held only by other profiles are inventory and are never probed with their credentials."
  } else if (effective === 'profile') {
    note = "Profile view: the active gateway profile, with global fallback per provider. Cards are not merged across profiles."

    if (requested === 'machine') {
      clamp = 'Profile view — this connection does not expose machine inventory'
    }
  } else {
    note = 'Requested ' + requested + ' view; effective scope not yet confirmed.'
  }

  return h(
    Zone,
    {
      id: 'subscriptions',
      title: 'Subscriptions',
      badge: zoneBadge(rs),
      controls: h(
        'div',
        { role: 'group', 'aria-label': 'Subscription scope', 'data-view-toggle': requested },
        h(ui.SegmentedControl, {
          options: [
            { id: 'machine', label: 'Machine' },
            { id: 'profile', label: 'Profile' }
          ],
          value: requested,
          onChange: props.onView,
          sizing: 'content'
        })
      ),
      note
    },
    clamp ? h('p', { role: 'status', 'data-view-clamp': 'profile', style: { ...SMALL, margin: '0 0 0.75rem', fontWeight: 500 } }, clamp) : null,
    content
  )
}

const gridStyle = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fill, minmax(min(100%, 19rem), 1fr))',
  gap: '0.75rem'
}

// ---------------------------------------------------------------------------
// Zone 2: usage analytics (native SVG)
// ---------------------------------------------------------------------------

function LineChart(props) {
  const W = 640
  const H = 190
  const L = 64
  const R = 14
  const T = 12
  const B = 30
  const pts = sortedByDay(props.points)
  const all = []

  props.lines.forEach(ln => {
    splitSegments(pts, ln.field).forEach(seg => seg.forEach(p => all.push(p.v)))
  })

  if (all.length === 0) {
    return h(
      'div',
      { 'data-chart-empty': props.title, style: { padding: '0.5rem 0' } },
      h('strong', { style: { fontSize: '0.8rem' } }, props.title),
      h('p', { style: { ...MUTED, margin: 0 } }, 'No observations for this period.')
    )
  }

  const ts = pts.map(p => dayToMs(p.day))
  const tmin = Math.min(...ts)
  const tmax = Math.max(...ts)
  const ymax = niceMax(Math.max(...all))
  const x = t => (tmax === tmin ? L + (W - L - R) / 2 : L + ((t - tmin) / (tmax - tmin)) * (W - L - R))
  const y = v => T + (1 - v / ymax) * (H - T - B)
  const els = []

  ;[0, 0.5, 1].forEach((f, i) => {
    const v = ymax * f

    els.push(
      h(
        'g',
        { key: 'tick' + i },
        h('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), style: { stroke: STROKE, strokeWidth: 1 } }),
        h('text', { x: L - 6, y: y(v) + 4, textAnchor: 'end', style: { fill: TEXT_3, fontSize: '10px' } }, fmtNumber(v, props.unit))
      )
    )
  })
  els.push(h('text', { key: 'x0', x: L, y: H - 8, textAnchor: 'start', style: { fill: TEXT_3, fontSize: '10px' } }, pts[0].day))

  if (pts.length > 1) {
    els.push(h('text', { key: 'x1', x: W - R, y: H - 8, textAnchor: 'end', style: { fill: TEXT_3, fontSize: '10px' } }, pts[pts.length - 1].day))
  }

  props.lines.forEach(ln => {
    const lineStyle = { stroke: ln.stroke, strokeWidth: 2, strokeDasharray: ln.dash, fill: 'none' }

    splitSegments(pts, ln.field).forEach((seg, si) => {
      if (seg.length > 1) {
        els.push(
          h('polyline', {
            key: ln.field + '-s' + si,
            style: lineStyle,
            points: seg.map(p => x(p.t).toFixed(1) + ',' + y(p.v).toFixed(1)).join(' ')
          })
        )
      }

      seg.forEach(p => {
        els.push(
          h(
            'circle',
            { key: ln.field + '-' + p.day, cx: x(p.t), cy: y(p.v), r: 3, style: { fill: ln.stroke } },
            h('title', null, p.day + ' — ' + ln.name + ': ' + fmtNumber(p.v, props.unit))
          )
        )
      })
    })
  })

  return h(
    'figure',
    { 'data-chart': props.title, style: { margin: 0, minWidth: 0 } },
    h(
      'figcaption',
      { style: { fontSize: '0.8rem', fontWeight: 500, display: 'flex', flexWrap: 'wrap', gap: '0.25rem 0.75rem', alignItems: 'center' } },
      props.title,
      h(
        'span',
        { style: { display: 'inline-flex', gap: '0.75rem', ...MUTED } },
        props.lines.map(ln =>
          h(
            'span',
            { key: ln.field, style: { display: 'inline-flex', alignItems: 'center', gap: '0.25rem' } },
            h('svg', { width: 22, height: 8, 'aria-hidden': 'true' }, h('line', { x1: 0, x2: 22, y1: 4, y2: 4, style: { stroke: ln.stroke, strokeWidth: 2, strokeDasharray: ln.dash } })),
            ln.name
          )
        )
      )
    ),
    h(
      'svg',
      {
        viewBox: '0 0 ' + W + ' ' + H,
        role: 'img',
        'aria-label': props.title + ', ' + pts.length + ' days with data',
        style: { width: '100%', height: 'auto', maxWidth: '100%' }
      },
      els
    )
  )
}

// Table cells shared by the dimension tables.
const TH = { textAlign: 'left', padding: '0.15rem 0.5rem', ...MUTED }
const TD = { padding: '0.15rem 0.5rem', fontSize: '0.75rem' }

// A metric cell: dash for unavailable, 0 for a measured zero, `*` on a known subtotal that misses sessions.
function MetricCell(props) {
  const m = props.metrics
  const v = num(m && m[props.field])
  const partial = v !== null && Array.isArray(m.missing_fields) && m.missing_fields.includes(props.field)

  return h(
    'td',
    {
      'data-field': props.field,
      'data-partial': partial ? 'true' : undefined,
      title: partial ? 'Known subtotal: some sessions did not record this value.' : v === null ? 'Not recorded.' : undefined,
      style: TD
    },
    fmtMetric(v, props.unit),
    partial ? h('span', { 'aria-hidden': 'true' }, '*') : null
  )
}

function TotalTokensCell(props) {
  const t = totalTokens(props.metrics)

  return h(
    'td',
    {
      'data-field': 'total_tokens',
      'data-partial': t.partial ? 'true' : undefined,
      title: t.partial ? 'Known subtotal: only one of input / output was recorded.' : undefined,
      style: TD
    },
    t.value === null ? '—' : fmtNumber(t.value, 'count'),
    t.partial ? h('span', { 'aria-hidden': 'true' }, '*') : null
  )
}

// Visible API-equivalent qualifiers (partial / unpriced / price proxy / stale prices).
const API_FLAG_STYLE = { ...SMALL, marginLeft: '0.4rem', padding: '0 0.35rem', border: '1px dotted ' + STROKE, borderRadius: '999px', color: TEXT_3 }

function ApiFlags(props) {
  return apiFlags(props.view).map(f => h('span', { key: f.id, 'data-api-flag': f.id, style: API_FLAG_STYLE }, f.text))
}

const apiAmountStyle = v => ({ fontVariantNumeric: 'tabular-nums', ...(v.state === 'null' ? { color: TEXT_3, fontStyle: 'italic' } : {}) })

// API-equivalent ($) cell: dash for null, $0.00 for an exact zero (data-api-state tells them apart).
function ApiCell(props) {
  const v = apiView(props.metrics)

  return h(
    'td',
    {
      'data-field': 'api_equivalent_usd',
      'data-api-state': v.state,
      'data-api-pico': v.pico === null ? undefined : v.pico,
      'data-partial': v.partial ? 'true' : undefined,
      'data-row-count': typeof props.rowCount === 'number' ? String(props.rowCount) : undefined,
      title: apiTitle(v, props.row, props.rowCount),
      style: TD
    },
    h('span', { style: apiAmountStyle(v) }, v.text),
    h(ApiFlags, { view: v })
  )
}

// The same value inside the totals card.
function ApiTotal(props) {
  const v = apiView(props.metrics)

  return h(
    'span',
    {
      'data-field': 'api_equivalent_usd',
      'data-api-state': v.state,
      'data-api-pico': v.pico === null ? undefined : v.pico,
      'data-partial': v.partial ? 'true' : undefined,
      title: apiTitle(v, null, props.rowCount)
    },
    h('span', { style: apiAmountStyle(v) }, v.text),
    h(ApiFlags, { view: v })
  )
}

// Explicit unpriced bucket: exact model ids no price could be found for, with the reason; never summed.
function UnpricedModels(props) {
  const groups = unpricedModels(props.data)

  if (groups.length === 0) {
    return null
  }

  const total = groups.reduce((n, g) => n + g.rows, 0)

  return h(
    'details',
    { 'data-unpriced-models': 'true', style: { marginTop: '0.5rem' } },
    h('summary', { style: { ...SMALL, cursor: 'pointer' } }, 'Unpriced models (' + total + ' daily row' + (total === 1 ? '' : 's') + ')'),
    h('p', { style: { ...MUTED, margin: '0.25rem 0' } }, 'These rows have no API-equivalent value. They are shown as \u2014 and are excluded from every sum; they are not zero.'),
    h(
      'ul',
      { style: { ...SMALL, margin: 0, paddingLeft: '1.1rem' } },
      groups.map(g =>
        h(
          'li',
          { key: g.model + '|' + (g.code || ''), 'data-unpriced-model': g.model, 'data-reason': g.code || undefined },
          g.model + ' \u2014 ' + g.reason + ' (' + g.rows + ' daily row' + (g.rows === 1 ? '' : 's') + ')'
        )
      )
    )
  )
}

function PricingNotes(props) {
  return pricingNotes(props.data).map(n => h('p', { key: n.code, 'data-pricing-warning': n.code, style: { ...MUTED, margin: '0.25rem 0 0' } }, n.text))
}

const apiHead = c => h('th', { key: c, scope: 'col', title: c === API_LABEL ? API_TIP : undefined, style: TH }, c)

// Subordinate line of the totals area: completed-session duration percentiles (null stays a dash, never 0).
function DurationLine(props) {
  const dp = props.depth

  if (!dp || !dp.supported) {
    return null
  }

  if (dp.durations === null) {
    return h('p', { 'data-duration-summary': 'unavailable', style: { ...MUTED, margin: '0.25rem 0 0' } }, DURATION_LABEL + ': unavailable (session stores could not be read).')
  }

  const out = [DURATION_LABEL + ': ']

  durationItems(dp.durations, dp.samples).forEach((it, i) => {
    if (i > 0) {
      out.push(' · ')
    }

    out.push(h('span', { key: it.id, title: it.title, 'data-duration': it.id }, it.label + ' ' + it.text))
  })

  return h('p', { 'data-duration-summary': 'true', style: { ...MUTED, margin: '0.25rem 0 0' } }, out)
}

// Whole-scope totals: independent of the dimension, the top-N cut and the legend.
function TotalsBar(props) {
  const t = props.totals
  const partial = Array.isArray(t.missing_fields) && t.missing_fields.length > 0
  const tt = totalTokens(t)
  const items = [
    ['Input', fmtMetric(t.input_count, 'count')],
    ['Output', fmtMetric(t.output_count, 'count')],
    ['Total tokens', tt.value === null ? '—' : fmtNumber(tt.value, 'count')],
    ['Est. USD', fmtMetric(t.estimated_usd, 'usd')],
    [API_LABEL, h(ApiTotal, { metrics: t, rowCount: props.rowCount }), API_TIP],
    ['Sessions', fmtMetric(t.sessions, 'count')],
    ['API calls', fmtMetric(t.api_calls, 'count')]
  ]

  return h(
    'div',
    { 'data-usage-totals': 'true', style: { marginTop: '0.75rem', borderTop: '1px solid ' + STROKE, paddingTop: '0.5rem' } },
    h('p', { style: { ...MUTED, margin: 0 } }, 'Totals for the whole filtered scope, including any group hidden in the legend.'),
    h(
      'dl',
      { style: { display: 'flex', flexWrap: 'wrap', gap: '0.25rem 1.25rem', margin: '0.25rem 0 0' } },
      items.map(it =>
        h(
          'div',
          { key: it[0] },
          h('dt', { style: MUTED, title: it[2] }, it[0]),
          h('dd', { style: { margin: 0, fontSize: '0.85rem', fontWeight: 500 } }, it[1])
        )
      )
    ),
    h(DurationLine, { depth: props.depth }),
    h('p', { 'data-api-footnote': 'true', style: { ...MUTED, margin: '0.25rem 0 0' } }, API_FOOTNOTE),
    partial
      ? h(
          'p',
          { 'data-totals-partial': 'true', style: { ...MUTED, margin: '0.25rem 0 0' } },
          '* Some sessions did not record: ' + t.missing_fields.join(', ') + '. Those figures are known subtotals; nothing is estimated for the missing values.'
        )
      : null
  )
}

const HEAT_STEPS = [0, 0.18, 0.4, 0.62, 0.85]
const HEAT_CELL = { width: '1.55rem', height: '1.35rem', padding: 0, borderRadius: '3px', fontSize: '0.7rem', lineHeight: 1, cursor: 'pointer', color: 'inherit', font: 'inherit' }
const CARD_STYLE = { border: '1px solid ' + STROKE, borderRadius: '0.5rem', padding: '0.85rem', marginTop: '0.75rem', display: 'flex', flexDirection: 'column', gap: '0.5rem', minWidth: 0 }

function depthStatusNote(dp) {
  const line = depthStatusLine(dp.status)

  return line ? h('p', { 'data-depth-status': 'true', style: { ...MUTED, margin: 0 } }, line) : null
}

// Activity by start time: the exact start epochs are re-bucketed by the machine's local zone rules (never a rotated UTC grid).
function HeatmapCard(props) {
  const dp = props.depth
  const [metric, setMetric] = useState('sessions')
  const [sel, setSel] = useState(null)
  const starts = dp && dp.heatmap ? dp.heatmap.starts : null
  const cells = useMemo(() => (starts ? localHeat(starts) : null), [starts])

  if (!dp || !dp.supported) {
    return null
  }

  const partial = !!(dp.status && dp.status.partial)
  let body

  if (dp.heatmap === null) {
    body = h('p', { 'data-depth-state': 'unavailable', style: MUTED }, 'Activity heatmap unavailable: the session stores could not be read.')
  } else if (cells === null) {
    body = h('p', { 'data-depth-state': 'no-local', style: MUTED }, 'Local heatmap unavailable')
  } else {
    const max = heatMax(cells, metric)
    const picked = sel && cells[sel.row * 24 + sel.col]
    const detail = sel ? heatDetail(starts, sel.row, sel.col) : []

    body = h(
      'div',
      null,
      h(
        'div',
        { style: { overflowX: 'auto' } },
        h(
          'table',
          { 'data-heat-grid': 'local', 'data-heat-metric': metric, style: { borderCollapse: 'separate', borderSpacing: '2px' } },
          h('caption', { style: SR_ONLY }, 'Sessions started per local weekday and hour'),
          h('thead', null, h('tr', null, h('th', { scope: 'col', style: TH }, 'Day'), Array.from({ length: 24 }, (_, c) => h('th', { key: c, scope: 'col', style: { ...TH, textAlign: 'center', padding: '0 0.15rem' } }, depthPad(c))))),
          h(
            'tbody',
            null,
            DEPTH_DAYS.map((day, r) =>
              h(
                'tr',
                { key: day },
                h('th', { scope: 'row', style: TH }, day),
                Array.from({ length: 24 }, (_, c) => {
                  const cell = cells[r * 24 + c]
                  const lv = heatLevel(cell, metric, max)
                  const label = heatCellLabel(r, c, cell, partial)
                  const on = !!sel && sel.row === r && sel.col === c

                  return h(
                    'td',
                    { key: c, style: { padding: 0 } },
                    h(
                      'button',
                      {
                        type: 'button',
                        'aria-label': label,
                        title: label,
                        'aria-pressed': on ? 'true' : 'false',
                        'data-heat-cell': r + '-' + c,
                        'data-sessions': String(cell.sessions),
                        'data-tokens': cell.tokens === null ? 'null' : String(cell.tokens),
                        'data-heat-na': lv.na ? 'true' : undefined,
                        onClick: () => setSel(on ? null : { row: r, col: c }),
                        style: {
                          ...HEAT_CELL,
                          border: (lv.na ? '1px dashed ' : '1px solid ') + (on ? ACCENT : STROKE),
                          background: lv.level > 0 ? 'color-mix(in srgb, ' + ACCENT + ' ' + Math.round(HEAT_STEPS[lv.level] * 100) + '%, transparent)' : 'transparent'
                        }
                      },
                      lv.na ? '?' : cell.sessions > 0 ? String(cell.sessions) : ''
                    )
                  )
                })
              )
            )
          )
        )
      ),
      sel && picked
        ? h(
            'div',
            { 'data-heat-detail': sel.row + '-' + sel.col, role: 'region', 'aria-label': 'Sessions started ' + DEPTH_DAYS[sel.row] + ' ' + depthPad(sel.col) + ':00', style: { borderTop: '1px solid ' + STROKE, paddingTop: '0.5rem' } },
            h('p', { style: { ...SMALL, margin: 0 } }, heatCellLabel(sel.row, sel.col, picked, partial)),
            detail.length === 0
              ? h('p', { style: MUTED }, 'No sessions started in this hour.')
              : h(
                  'table',
                  { style: { borderCollapse: 'collapse' } },
                  h('thead', null, h('tr', null, ['Start (instant)', 'Local offset', 'Sessions', 'Tokens'].map(c => h('th', { key: c, scope: 'col', style: TH }, c)))),
                  h(
                    'tbody',
                    null,
                    detail.map(e =>
                      h(
                        'tr',
                        { key: e.t, 'data-heat-start': String(e.t) },
                        h('td', { style: TD }, e.iso),
                        h('td', { style: TD }, e.offset),
                        h('td', { style: TD }, String(e.sessions)),
                        h('td', { style: TD }, e.tokens === null ? '—' : depthGroup(e.tokens))
                      )
                    )
                  )
                )
          )
        : null
    )
  }

  return h(
    'section',
    { 'data-depth-card': 'heatmap', 'aria-labelledby': 'ud-h-heat', style: CARD_STYLE },
    h('h3', { id: 'ud-h-heat', style: { margin: 0, fontSize: '0.95rem', fontWeight: 600 } }, 'Activity by start time (local)'),
    h('p', { 'data-heat-caption': 'true', style: { ...MUTED, margin: 0 } }, 'Time zone: ' + localZoneName() + '. ' + HEAT_NOTE),
    h(
      'div',
      { role: 'group', 'aria-label': 'Heatmap intensity', style: { display: 'flex', gap: '0.4rem' } },
      ['sessions', 'tokens'].map(m =>
        h(
          ui.Button,
          { key: m, type: 'button', variant: metric === m ? 'default' : 'outline', size: 'sm', 'aria-pressed': metric === m ? 'true' : 'false', 'data-heat-toggle': m, onClick: () => setMetric(m) },
          m === 'sessions' ? 'Sessions' : 'Tokens'
        )
      )
    ),
    body,
    depthStatusNote(dp)
  )
}

// Cache economics per exact model id (recorded-cost basis); provider is a displayed attribute (resolved wins over Unknown): no top-N, no price fetch, no JS money.
function CacheCard(props) {
  const dp = props.depth

  if (!dp || !dp.supported) {
    return null
  }

  const cols = ['Model', 'Provider', 'Hit %', 'Cached input', 'Uncached input', 'Cache writes', 'Reasoning', 'Reasoning share', CACHE_LABEL, 'Details']
  let body

  if (dp.cache === null) {
    body = h('p', { 'data-depth-state': 'unavailable', style: MUTED }, 'Cache economics unavailable: the session stores could not be read.')
  } else if (dp.cache.length === 0) {
    body = h('p', { 'data-depth-state': 'empty', style: MUTED }, 'No sessions in this scope.')
  } else {
    body = h(
      'div',
      { style: { overflow: 'auto', maxHeight: '24rem' } },
      h(
        'table',
        { 'data-cache-table': 'true', style: { borderCollapse: 'collapse', width: '100%' } },
        h('thead', null, h('tr', null, cols.map(c => h('th', { key: c, scope: 'col', title: c === CACHE_LABEL ? CACHE_TIP : undefined, style: TH }, c)))),
        h(
          'tbody',
          null,
          dp.cache.map((r, i) => {
            const v = cacheRowView(r)

            return h(
              'tr',
              { key: i, 'data-cache-row': v.model + '|' + (v.provider || ''), 'data-cache-reason': v.reason || undefined },
              h('td', { style: TD }, v.model),
              h('td', { style: TD, 'data-field': 'provider_id' }, v.provider === null ? 'Unknown' : providerList(v.provider).map(function (id) { return providerLabelOf(props.providers, id); }).join(', ')),
              h('td', { 'data-field': 'hit_rate_pct', style: TD }, v.hit),
              h('td', { 'data-field': 'cached_input_tokens', style: TD }, v.cached),
              h('td', { 'data-field': 'uncached_input_tokens', style: TD }, v.uncached),
              h('td', { 'data-field': 'cache_write_tokens', style: TD }, v.writes),
              h('td', { 'data-field': 'reasoning_tokens', style: TD }, v.reasoning),
              h('td', { 'data-field': 'reasoning_share_pct', style: TD }, v.share),
              h('td', { 'data-field': 'effective_cost_per_mtok', title: v.effectiveTitle, style: TD }, v.effective),
              h(
                'td',
                { style: TD },
                h(
                  'details',
                  null,
                  h('summary', { style: { ...SMALL, cursor: 'pointer' } }, 'Details'),
                  h(
                    'dl',
                    { style: { ...SMALL, margin: '0.3rem 0 0' } },
                    h('dt', { style: MUTED }, 'Output tokens'),
                    h('dd', { 'data-field': 'output_tokens', style: { margin: 0 } }, v.output),
                    h('dt', { style: MUTED }, 'Cache discount multiplier'),
                    h('dd', { 'data-field': 'discount_multiplier', style: { margin: 0 } }, v.multiplier),
                    h('dt', { style: MUTED }, 'Rate snapshot'),
                    h('dd', { 'data-field': 'rate_source', style: { margin: 0 } }, v.rateSource),
                    v.reasonText ? h('dt', { style: MUTED }, 'Why a value is missing') : null,
                    v.reasonText ? h('dd', { 'data-field': 'reason', style: { margin: 0 } }, v.reasonText) : null
                  )
                )
              )
            )
          })
        )
      )
    )
  }

  return h(
    'section',
    { 'data-depth-card': 'cache', 'aria-labelledby': 'ud-h-cache', style: CARD_STYLE },
    h('h3', { id: 'ud-h-cache', style: { margin: 0, fontSize: '0.95rem', fontWeight: 600 } }, 'Cache economics · recorded-cost basis'),
    h('p', { style: { ...MUTED, margin: 0 } }, CACHE_NOTE + ' ' + CACHE_TIP),
    body,
    depthStatusNote(dp)
  )
}

// Per-group duration table and repository path detail (text nodes only).
function GroupDurations(props) {
  const dp = props.depth

  if (!dp || !dp.supported) {
    return null
  }

  const withStats = props.views.filter(v => isObj(v.group.durations))

  if (withStats.length === 0) {
    return null
  }

  return h(
    'details',
    { 'data-group-durations': 'true' },
    h('summary', { style: { ...SMALL, cursor: 'pointer' } }, DURATION_LABEL + ' by group'),
    h(
      'div',
      { style: { overflowX: 'auto' } },
      h(
        'table',
        { style: { borderCollapse: 'collapse', width: '100%' } },
        h('thead', null, h('tr', null, ['Group', 'p50', 'p90', 'p99', 'mean', 'max', 'samples'].map(c => h('th', { key: c, scope: 'col', style: TH }, c)))),
        h(
          'tbody',
          null,
          withStats.map(v =>
            h(
              'tr',
              { key: v.group.id, 'data-group-id': v.group.id },
              h('td', { style: TD }, str(v.group.label)),
              durationItems(v.group.durations, v.group.duration_samples).map(it => h('td', { key: it.id, title: it.title, 'data-duration': it.id, style: TD }, it.text))
            )
          )
        )
      )
    )
  )
}

function RepoPaths(props) {
  const dp = props.depth

  if (!dp || !dp.repos) {
    return null
  }

  const items = props.views.map(v => ({ v, p: repoPathSummary(repoRowFor(v.group, dp.repos)) })).filter(x => x.p.count > 0)

  if (items.length === 0) {
    return null
  }

  return h(
    'details',
    { 'data-repo-paths': 'true' },
    h('summary', { style: { ...SMALL, cursor: 'pointer' } }, 'Repository paths (' + items.length + ')'),
    h(
      'ul',
      { style: SMALL },
      items.map(x =>
        h(
          'li',
          { key: x.v.group.id, 'data-repo-group': x.v.group.id },
          x.p.count === 1
            ? str(x.v.group.label) + ' — ' + x.p.text
            : h('details', null, h('summary', null, str(x.v.group.label) + ' — ' + x.p.text), h('ul', null, x.p.paths.map(p => h('li', { key: p }, p))))
        )
      )
    )
  )
}

function CoverageNote(props) {
  const d = props.data

  if (d.complete) {
    return null
  }

  const bad = d.sources.filter(x => x.state === 'unavailable' || x.state === 'excluded')

  return h(
    'div',
    { role: 'status', 'data-coverage': 'incomplete', style: { border: '1px solid ' + STROKE, borderRadius: '6px', padding: '0.5rem 0.75rem', marginBottom: '0.5rem' } },
    h('div', { style: { fontSize: '0.8rem', fontWeight: 500 } }, '⚠ ' + (d.mode === 'machine' ? 'Incomplete machine coverage' : 'Incomplete coverage')),
    h('p', { style: { ...SMALL, margin: '0.25rem 0' } }, 'Totals below cover only the session stores that could be read; nothing is guessed for the others.'),
    h(
      'ul',
      { 'data-sources': 'true', style: { ...SMALL, margin: 0, paddingLeft: '1.1rem' } },
      d.sources.map((x, i) =>
        h('li', { key: i, 'data-source-state': str(x.state) || undefined }, (x.is_global === true ? 'global (default home)' : str(x.profile) || 'profile') + ' — ' + (str(x.state) || 'unknown'))
      )
    ),
    bad.length === 0 ? h('p', { style: { ...MUTED, margin: '0.25rem 0 0' } }, 'Some profile stores were not enumerated (safety bound or unreadable directory).') : null
  )
}

// Daily provenance rows: which profile DB, model and provider each contribution came from.
function RowsTable(props) {
  const rows = props.rows.slice().sort((a, b) => (a.day < b.day ? -1 : a.day > b.day ? 1 : 0) || (str(a.profile) < str(b.profile) ? -1 : 1))
  const shown = rows.slice(0, 500)
  const cols = (props.withGroup ? ['Group'] : []).concat(['Day', 'Profile DB', 'Model', 'Provider', 'Input', 'Output', 'Est. USD', API_LABEL, 'Sessions', 'API calls'])

  return h(
    'div',
    { style: { overflowX: 'auto' } },
    h(
      'table',
      { 'data-rows-table': props.name || 'rows', style: { borderCollapse: 'collapse', width: '100%' } },
      h('thead', null, h('tr', null, cols.map(apiHead))),
      h(
        'tbody',
        null,
        shown.map(r => {
          const attr = str(r.attribution_source)
          const note = attr === 'billing_provider' ? ' (billing)' : attr === 'models_json' ? ' (catalog)' : ''

          return h(
            'tr',
            { key: r.id, 'data-row-id': r.id },
            props.withGroup ? h('td', { style: TD }, props.groupLabel(r)) : null,
            h('td', { style: TD }, r.day),
            h('td', { style: TD }, profileDbLabel(r)),
            h('td', { style: TD }, str(r.model_id) || 'Unknown model'),
            h('td', { 'data-attribution': attr || undefined, title: has(ATTRIBUTION_TIP, attr) ? ATTRIBUTION_TIP[attr] : undefined, style: TD }, providerLabelOf(props.providers, r.provider_id) + note),
            h(MetricCell, { metrics: r.metrics, field: 'input_count' }),
            h(MetricCell, { metrics: r.metrics, field: 'output_count' }),
            h(MetricCell, { metrics: r.metrics, field: 'estimated_usd', unit: 'usd' }),
            h(ApiCell, { metrics: r.metrics, row: r }),
            h(MetricCell, { metrics: r.metrics, field: 'sessions' }),
            h(MetricCell, { metrics: r.metrics, field: 'api_calls' })
          )
        })
      )
    ),
    rows.length > shown.length ? h('p', { style: MUTED }, 'Showing the first ' + shown.length + ' of ' + rows.length + ' daily rows.') : null
  )
}

// One chart over several groups: every line has its own points; null breaks a line, never a zero.
function GroupChart(props) {
  const W = 640
  const H = 190
  const L = 64
  const R = 14
  const T = 12
  const B = 30
  const all = []
  const days = []

  props.lines.forEach(ln => {
    splitSegments(ln.points, ln.field).forEach(seg =>
      seg.forEach(p => {
        all.push(p.v)
        days.push(p.t)
      })
    )
  })

  if (all.length === 0) {
    return h(
      'div',
      { 'data-chart-empty': props.title, style: { padding: '0.5rem 0' } },
      h('strong', { style: { fontSize: '0.8rem' } }, props.title),
      h('p', { style: { ...MUTED, margin: 0 } }, 'No observations for this period.')
    )
  }

  const tmin = Math.min(...days)
  const tmax = Math.max(...days)
  const ymax = niceMax(Math.max(...all))
  const x = t => (tmax === tmin ? L + (W - L - R) / 2 : L + ((t - tmin) / (tmax - tmin)) * (W - L - R))
  const y = v => T + (1 - v / ymax) * (H - T - B)
  const els = []

  ;[0, 0.5, 1].forEach((f, i) => {
    const v = ymax * f

    els.push(
      h(
        'g',
        { key: 'tick' + i },
        h('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), style: { stroke: STROKE, strokeWidth: 1 } }),
        h('text', { x: L - 6, y: y(v) + 4, textAnchor: 'end', style: { fill: TEXT_3, fontSize: '10px' } }, fmtNumber(v, props.unit))
      )
    )
  })
  els.push(h('text', { key: 'x0', x: L, y: H - 8, textAnchor: 'start', style: { fill: TEXT_3, fontSize: '10px' } }, new Date(tmin).toISOString().slice(0, 10)))

  if (tmax !== tmin) {
    els.push(h('text', { key: 'x1', x: W - R, y: H - 8, textAnchor: 'end', style: { fill: TEXT_3, fontSize: '10px' } }, new Date(tmax).toISOString().slice(0, 10)))
  }

  props.lines.forEach(ln => {
    const lineStyle = { stroke: ln.stroke, strokeWidth: 2, strokeDasharray: ln.dash, fill: 'none' }

    splitSegments(ln.points, ln.field).forEach((seg, si) => {
      if (seg.length > 1) {
        els.push(
          h('polyline', {
            key: ln.key + '-s' + si,
            'data-group-line': ln.groupId,
            'data-metric': ln.field,
            style: lineStyle,
            points: seg.map(p => x(p.t).toFixed(1) + ',' + y(p.v).toFixed(1)).join(' ')
          })
        )
      }

      seg.forEach(p => {
        els.push(
          h(
            'circle',
            { key: ln.key + '-' + p.day, 'data-group-line': ln.groupId, 'data-metric': ln.field, cx: x(p.t), cy: y(p.v), r: 3, style: { fill: ln.stroke } },
            h('title', null, ln.tip(p))
          )
        )
      })
    })
  })

  return h(
    'figure',
    { 'data-chart': props.title, style: { margin: 0, minWidth: 0 } },
    h(
      'figcaption',
      { style: { fontSize: '0.8rem', fontWeight: 500, display: 'flex', flexWrap: 'wrap', gap: '0.25rem 0.75rem', alignItems: 'center' } },
      props.title,
      props.note ? h('span', { style: MUTED }, props.note) : null
    ),
    h(
      'svg',
      { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': props.title + ', ' + props.lines.length + ' lines', style: { width: '100%', height: 'auto', maxWidth: '100%' } },
      els
    )
  )
}

function pointTip(view, p) {
  const owners = (Array.isArray(view.group.profiles) ? view.group.profiles : []).map(o => (o.is_global === true ? 'global' : str(o.profile))).filter(Boolean)

  return (
    str(view.group.label) +
    ' — ' +
    p.day +
    ': input ' +
    fmtMetric(p.input_count) +
    ', output ' +
    fmtMetric(p.output_count) +
    ', est. ' +
    fmtMetric(p.estimated_usd, 'usd') +
    (owners.length ? '; profiles: ' + owners.join(', ') : '')
  )
}

// By profile / By model / By repo: one shared legend drives both charts and the grouped tables (no refetch).
function GroupedView(props) {
  const d = props.data
  const views = useMemo(() => groupViews(d), [d])
  const [hidden, setHidden] = useState({})
  const visible = views.filter(v => !hidden[v.group.id])
  const anyHidden = views.some(v => hidden[v.group.id])
  const byProfile = d.dimension === 'profile'
  const byRepo = d.dimension === 'repo'
  const noun = byRepo ? 'repository' : byProfile ? 'profile' : 'model'
  const depthInfo = d.depth || null
  const repoOf = v => (byRepo && depthInfo && depthInfo.repos ? repoPathSummary(repoRowFor(v.group, depthInfo.repos)) : null)
  const coverage = byRepo && depthInfo && depthInfo.repos ? repoCoverage(depthInfo.repos) : null
  const tokenLines = []
  const spendLines = []
  const toggle = id =>
    setHidden(prev => {
      const n = { ...prev }

      if (n[id]) {
        delete n[id]
      } else {
        n[id] = true
      }

      return n
    })

  visible.forEach(v => {
    const pts = sortedByDay(v.series.points)
    const stroke = groupStroke(v.color)
    const base = { stroke, groupId: v.group.id, points: pts, tip: p => pointTip(v, p) }

    tokenLines.push({ key: v.group.id + '-in', field: 'input_count', dash: undefined, ...base })
    tokenLines.push({ key: v.group.id + '-out', field: 'output_count', dash: '4 3', ...base })
    spendLines.push({ key: v.group.id + '-usd', field: 'estimated_usd', dash: undefined, ...base })
  })

  const otherView = visible.filter(v => v.group.is_other === true)[0] || null
  const groupOfRow = {}
  const mainRows = []

  views.forEach(v => v.rows.forEach(r => (groupOfRow[r.id] = v.group)))
  visible.forEach(v => {
    if (v.group.is_other !== true) {
      v.rows.forEach(r => mainRows.push(r))
    }
  })

  return h(
    'div',
    { 'data-dimension-view': d.dimension, style: { display: 'flex', flexDirection: 'column', gap: '0.75rem' } },
    h(
      'div',
      { role: 'group', 'aria-label': byRepo ? 'Repositories' : byProfile ? 'Profiles' : 'Models', style: { display: 'flex', flexWrap: 'wrap', gap: '0.4rem' } },
      views.map(v =>
        h(
          ui.Button,
          {
            key: v.group.id,
            type: 'button',
            variant: hidden[v.group.id] ? 'outline' : 'default',
            size: 'sm',
            'aria-pressed': hidden[v.group.id] ? 'false' : 'true',
            'data-group-id': v.group.id,
            onClick: () => toggle(v.group.id)
          },
          h('svg', { width: 22, height: 8, 'aria-hidden': 'true', style: { marginRight: '0.35rem' } }, h('line', { x1: 0, x2: 22, y1: 4, y2: 4, style: { stroke: groupStroke(v.color), strokeWidth: 2 } })),
          str(v.group.label)
        )
      ),
      h(ui.Button, { type: 'button', variant: 'outline', size: 'sm', disabled: !anyHidden, 'data-show-all': 'true', onClick: () => setHidden({}) }, 'Show all')
    ),
    d.dimension === 'model' && d.top_n
      ? h('p', { style: { ...MUTED, margin: 0 } }, 'Top ' + d.top_n + ' models by known input + output tokens over this window; every remaining model is rolled up into Other.')
      : null,
    h(
      'div',
      { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 22rem), 1fr))', gap: '1rem' } },
      h(GroupChart, { title: 'Tokens (count)', unit: 'count', lines: tokenLines, note: 'solid = input · dashed = output' }),
      h(GroupChart, { title: 'Estimated spend (USD)', unit: 'usd', lines: spendLines })
    ),
    h(
      'div',
      { style: { overflowX: 'auto' } },
      h(
        'table',
        { 'data-summary-table': d.dimension, style: { borderCollapse: 'collapse', width: '100%' } },
        h('thead', null, h('tr', null, [byRepo ? 'Repository' : 'Group', 'Input', 'Output', 'Total tokens', 'Est. USD', API_LABEL, 'Sessions', 'API calls'].map(apiHead))),
        h(
          'tbody',
          null,
          visible.map(v => {
            const repo = repoOf(v)

            return h(
              'tr',
              { key: v.group.id, 'data-group-id': v.group.id },
              h('td', { style: TD }, str(v.group.label), repo && repo.text ? h('div', { 'data-repo-path': repo.count === 1 ? 'single' : 'multi', style: MUTED }, repo.text) : null),
              h(MetricCell, { metrics: v.group.totals, field: 'input_count' }),
              h(MetricCell, { metrics: v.group.totals, field: 'output_count' }),
              h(TotalTokensCell, { metrics: v.group.totals }),
              h(MetricCell, { metrics: v.group.totals, field: 'estimated_usd', unit: 'usd' }),
              h(ApiCell, { metrics: v.group.totals, rowCount: v.group.row_ids.length }),
              h(MetricCell, { metrics: v.group.totals, field: 'sessions' }),
              h(MetricCell, { metrics: v.group.totals, field: 'api_calls' })
            )
          })
        )
      )
    ),
    byRepo && coverage ? h('p', { 'data-repo-coverage': String(coverage.pct), style: { ...MUTED, margin: 0 } }, coverage.text) : null,
    h(GroupDurations, { views, depth: depthInfo }),
    byRepo ? h(RepoPaths, { views, depth: depthInfo }) : null,
    h(
      'details',
      { 'data-grouped-rows': 'true' },
      h('summary', { style: { ...SMALL, cursor: 'pointer' } }, 'Daily rows by ' + noun + ' (' + mainRows.length + ')'),
      h(RowsTable, { rows: mainRows, providers: props.providers, withGroup: true, name: 'grouped', groupLabel: r => str(groupOfRow[r.id] && groupOfRow[r.id].label) })
    ),
    otherView
      ? h(
          'details',
          { 'data-other-rows': 'true' },
          h('summary', { style: { ...SMALL, cursor: 'pointer' } }, 'Other — ' + otherView.rows.length + ' daily rows from the models rolled up into Other'),
          h(RowsTable, { rows: otherView.rows, providers: props.providers, name: 'other' })
        )
      : null
  )
}

function SeriesBlock(props) {
  const s = props.series
  const conf = effectiveConfidence(s.confidence, has(SOURCE_CEILING, s.source) ? SOURCE_CEILING[s.source] : 'opaque')
  const sorted = sortedByDay(s.points)
  const tip =
    'Source: Hermes session analytics. Scope: ' +
    (s.scope === 'machine' ? 'all local profiles' : s.scope === 'profile' ? 'this Hermes profile' : 'provider-attributed') +
    '. Period: ' +
    (isObj(s.period) && s.period.start ? fmtTime(s.period.start) + ' to ' + fmtTime(s.period.end) : 'rolling window') +
    '. Checked: ' +
    fmtTime(s.checked_at) +
    '.'

  if (isObj(s.error)) {
    const code = has(DECK_ERROR_TEXT, s.error.code) ? s.error.code : 'internal_error'

    return h(ErrorBlock, { what: 'this series', error: new UdError('deck', { code }) })
  }

  const th = c => h('th', { key: c, scope: 'col', style: { textAlign: 'left', padding: '0.15rem 0.5rem', ...MUTED } }, c)
  const td = (k, v) => h('td', { key: k, style: { padding: '0.15rem 0.5rem', fontSize: '0.75rem' } }, v)

  return h(
    'div',
    { 'data-series-id': str(s.id) || undefined },
    h(
      'div',
      { style: { display: 'flex', flexWrap: 'wrap', gap: '0.25rem 0.5rem', alignItems: 'center', marginBottom: '0.5rem' } },
      h(ConfBadge, { conf, tip }),
      h(ui.Badge, { variant: 'outline' }, s.scope === 'machine' ? 'All local profiles' : s.scope === 'profile' ? 'This Hermes profile' : 'Provider-attributed'),
      h(
        'span',
        { style: MUTED },
        'Days in server local time · ' +
          (s.coverage === 'complete' ? 'complete coverage' : 'partial coverage (first day partial, telemetry may be incomplete)') +
          ' · checked ' +
          fmtTime(s.checked_at)
      ),
      s.stale === true ? h(StaleTag, { refreshing: false }) : null
    ),
    sorted.length === 0
      ? h('p', { style: { ...SMALL, margin: 0 } }, REASON_TEXT.no_observations)
      : h(
          'div',
          { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 22rem), 1fr))', gap: '1rem' } },
          h(LineChart, {
            title: 'Tokens (count)',
            unit: 'count',
            points: sorted,
            lines: [
              { name: 'Input', field: 'input_count', stroke: ACCENT, dash: undefined },
              { name: 'Output', field: 'output_count', stroke: TEXT_2, dash: '4 3' }
            ]
          }),
          h(LineChart, {
            title: 'Estimated spend (USD)',
            unit: 'usd',
            points: sorted,
            lines: [{ name: 'Estimated USD', field: 'estimated_usd', stroke: ACCENT, dash: undefined }]
          })
        ),
    sorted.length === 0
      ? null
      : h(
          'details',
          { style: { marginTop: '0.5rem' } },
          h('summary', { style: { ...SMALL, cursor: 'pointer' } }, 'Data table'),
          h(
            'div',
            { style: { overflowX: 'auto' } },
            h(
              'table',
              { style: { borderCollapse: 'collapse', width: '100%' } },
              h('thead', null, h('tr', null, ['Day', 'Input', 'Output', 'Est. USD', API_LABEL, 'Sessions', 'API calls'].map(c => (c === API_LABEL ? apiHead(c) : th(c))))),
              h(
                'tbody',
                null,
                sorted.map(p =>
                  h(
                    'tr',
                    { key: p.day },
                    td('d', p.day),
                    td('i', fmtNumber(p.input_count, 'count')),
                    td('o', fmtNumber(p.output_count, 'count')),
                    td('u', fmtNumber(p.estimated_usd, 'usd')),
                    h(ApiCell, { key: 'q', metrics: p }),
                    td('s', fmtNumber(p.sessions, 'count')),
                    td('a', fmtNumber(p.api_calls, 'count'))
                  )
                )
              )
            )
          )
        ),
    props.rows && props.rows.length > 0
      ? h(
          'details',
          { 'data-provenance': 'true', style: { marginTop: '0.5rem' } },
          h('summary', { style: { ...SMALL, cursor: 'pointer' } }, 'Contributions by profile DB, model and provider (' + props.rows.length + ')'),
          h(RowsTable, { rows: props.rows, providers: props.providers, name: 'provenance' })
        )
      : null
  )
}

function UsageZone(props) {
  const rs = props.rs
  const providers = props.providers
  const selected = providers.filter(p => p.id === props.provider)[0]
  const d = rs.data
  const mode = d ? d.mode : props.view
  const dimension = d ? d.dimension : effectiveDimension(props.view, props.dimension)
  let content

  if (rs.phase === 'loading') {
    content = h(LoadingBlock, { rows: 5, label: 'Loading usage analytics…' })
  } else if (rs.phase === 'error') {
    content = h(ErrorBlock, { what: 'usage analytics', error: rs.error, onRetry: props.onRetry })
  } else {
    const err = rs.error ? h(ErrorBlock, { key: 'err', what: 'usage analytics', error: rs.error, onRetry: props.onRetry }) : null
    let inner

    if (d.series.length === 0) {
      const reasonKey = d.reason || 'no_observations'

      inner = h(
        'p',
        { 'data-chart-state': reasonKey, style: { ...SMALL, margin: 0 } },
        d.provider_id && reasonKey === 'no_observations' ? 'No observations for ' + providerLabelOf(d.providers, d.provider_id) + ' in this period.' : REASON_TEXT[reasonKey]
      )
    } else if (d.dimension === 'all') {
      inner = d.series.map((s, i) => h(SeriesBlock, { key: str(s.id) || i, series: s, rows: d.rows, providers: d.providers }))
    } else {
      inner = h(GroupedView, { key: [d.mode, d.dimension, d.window_days, d.provider_id].join('|'), data: d, providers: d.providers })
    }

    content = h('div', { 'aria-busy': rs.refreshing ? 'true' : undefined }, err, h(CoverageNote, { data: d }), inner, h(TotalsBar, { totals: d.totals, rowCount: d.rows.length, depth: d.depth }), h(HeatmapCard, { depth: d.depth }), h(CacheCard, { depth: d.depth, providers: d.providers }), h(PricingNotes, { data: d }), h(UnpricedModels, { data: d }))
  }

  return h(
    Zone,
    {
      id: 'usage',
      title: 'Usage analytics',
      badge: zoneBadge(rs),
      dataAttrs: { 'data-mode': mode, 'data-dimension': dimension },
      controls: [
        h(
          'div',
          { key: 'dimension', role: 'group', 'aria-label': 'Analytics dimension', 'data-dimension-toggle': dimension },
          h(ui.SegmentedControl, {
            options: dimensionOptions(mode),
            value: dimension,
            onChange: id => props.onDimension(id),
            sizing: 'content'
          })
        ),
        h(
          'div',
          { key: 'window', role: 'group', 'aria-label': 'Time window' },
          h(ui.SegmentedControl, {
            options: WINDOWS.map(w => ({ id: String(w), label: w + ' days' })),
            value: String(props.windowDays),
            onChange: id => props.onWindow(parseInt(id, 10)),
            sizing: 'content'
          })
        ),
        h(
          ui.Select,
          { key: 'provider', value: props.provider || ALL_PROVIDERS, onValueChange: v => props.onProvider(v === ALL_PROVIDERS ? '' : v) },
          h(ui.SelectTrigger, { 'aria-label': 'Provider filter', size: 'sm' }, h(ui.SelectValue, null)),
          h(
            ui.SelectContent,
            null,
            h(ui.SelectItem, { key: ALL_PROVIDERS, value: ALL_PROVIDERS }, 'All providers'),
            providers.map(p => h(ui.SelectItem, { key: p.id, value: p.id }, p.label))
          )
        )
      ],
      note: scopeCopy(mode) + (props.provider ? ' Filtered to ' + (selected ? selected.label : 'the selected provider') + '.' : '')
    },
    h('p', { 'data-attribution-limit': 'true', style: { ...MUTED, margin: '0 0 0.75rem' } }, COPY_ATTRIBUTION_LIMIT),
    content
  )
}

// ---------------------------------------------------------------------------
// Zone 3: self-hosted health strip
// ---------------------------------------------------------------------------

function HealthRow(props) {
  const e = props.entry
  const statusKey = has(HEALTH_META, e.status) ? e.status : 'unknown'
  const meta = HEALTH_META[statusKey]
  const metricsOk = e.metrics_available === true
  let conf = asConfidence(e.confidence)

  if (!metricsOk && conf === 'self_hosted') {
    conf = 'opaque' // no metrics, no self-hosted claim
  }

  const latency = num(e.latency_ms)
  const cpu = num(e.cpu_percent)
  const mem = num(e.memory_bytes)
  const reason = has(REASON_TEXT, e.reason) ? REASON_TEXT[e.reason] : null
  const stale = e.stale === true || props.zoneStale
  const errCode = isObj(e.error) ? (has(DECK_ERROR_TEXT, e.error.code) ? e.error.code : 'internal_error') : null
  const tip =
    'Source: ' +
    (metricsOk ? 'endpoint metrics and health probe' : 'endpoint health probe only') +
    '. Scope: endpoint. Observed: ' +
    fmtTime(e.checked_at) +
    '.'

  return h(
    'li',
    {
      'data-endpoint-id': str(e.endpoint_id) || undefined,
      'data-status': statusKey,
      style: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '0.25rem 0.75rem', padding: '0.4rem 0', borderTop: '1px solid ' + STROKE, listStyle: 'none' }
    },
    h(
      'span',
      { style: { display: 'inline-flex', alignItems: 'center', gap: '0.35rem', fontSize: '0.8rem', fontWeight: 500 } },
      h(ui.StatusDot, { tone: meta.tone }),
      h('span', { 'aria-hidden': 'true' }, meta.icon),
      meta.text
    ),
    h('span', { style: { fontSize: '0.8rem', overflowWrap: 'anywhere' } }, str(e.display_name) || str(e.provider_id) || 'Endpoint'),
    h(
      'span',
      { style: SMALL },
      latency === null ? 'Latency —' : Math.round(latency) + ' ms',
      cpu !== null ? ' · CPU ' + Math.round(cpu * 10) / 10 + '%' : null,
      mem !== null ? ' · Memory ' + fmtNumber(mem, 'bytes') : null
    ),
    h(ConfBadge, { conf, tip }),
    h('span', { style: MUTED }, 'Checked ' + fmtTime(e.checked_at)),
    stale ? h(StaleTag, { refreshing: false }) : null,
    !metricsOk && !errCode ? h('span', { style: MUTED }, 'Metrics unavailable') : null,
    reason && !errCode ? h('span', { style: MUTED }, reason) : null,
    e.reason === 'box_offline' ? h('span', { 'data-field': 'last-seen', style: MUTED }, lastSeenText(e)) : null,
    props.machine && e.profile_owned === true
      ? h('span', { style: MUTED, 'data-profile-owned': 'true', title: PROFILE_OWNED_NOTE }, PROFILE_OWNED_BADGE)
      : null,
    props.machine && statusKey === 'unknown' && e.reason === 'source_unavailable' && !errCode && e.profile_owned !== true
      ? h('span', { style: MUTED, 'data-machine-only-note': 'true', title: MACHINE_ONLY_NOTE }, 'Not probed')
      : null,
    errCode ? h('span', { role: 'note', style: { ...SMALL, fontWeight: 500 } }, '⚠ ' + deckErrorText(errCode)) : null
  )
}

function HealthZone(props) {
  const rs = props.rs
  const effective = props.effectiveView // 'machine' | 'profile' | null (not yet confirmed)
  let content

  if (rs.phase === 'loading') {
    content = h(LoadingBlock, { rows: 2, label: 'Loading endpoint health…' })
  } else if (rs.phase === 'error') {
    content = h(ErrorBlock, { what: 'endpoint health', error: rs.error, onRetry: props.onRetry })
  } else {
    const entries = rs.data.entries
    const err = rs.error ? h(ErrorBlock, { key: 'err', what: 'endpoint health', error: rs.error, onRetry: props.onRetry }) : null
    let empty

    if (effective === 'machine') {
      empty = 'No self-hosted endpoints detected on this machine.'
    } else if (effective === 'profile') {
      empty = 'No self-hosted endpoints detected in this profile.'
    } else {
      empty = 'No self-hosted endpoints reported for the requested view (effective scope not yet confirmed).'
    }

    content = h(
      'div',
      { 'aria-busy': rs.refreshing ? 'true' : undefined },
      err,
      entries.length === 0
        ? h('p', { style: { ...SMALL, margin: 0 } }, empty)
        : h(
            'ul',
            { style: { margin: 0, padding: 0 } },
            entries.map((e, i) => h(HealthRow, { key: str(e.endpoint_id) || i, entry: e, zoneStale: !!rs.error, machine: effective === 'machine' }))
          )
    )
  }

  return h(
    Zone,
    {
      id: 'health',
      title: 'Self-hosted health',
      badge: zoneBadge(rs),
      note: effective
        ? 'Scope: ' + effective + ' view (confirmed by the subscriptions response).'
        : 'Requested ' + props.requestedView + ' view; effective scope not yet confirmed.'
    },
    content
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

function PageShell(props) {
  return h(
    'div',
    { role: 'region', 'aria-label': 'UsageDeck', style: { height: '100%', overflowY: 'auto' } },
    h(
      'div',
      { style: { maxWidth: '1200px', margin: '0 auto', padding: '1rem 1.25rem 2rem', boxSizing: 'border-box' } },
      h(
        'header',
        { style: { display: 'flex', flexWrap: 'wrap', gap: '0.75rem', justifyContent: 'space-between', alignItems: 'flex-start' } },
        h(
          'div',
          { style: { minWidth: 0, flex: '1 1 20rem' } },
          h('h1', { style: { fontSize: '1.25rem', fontWeight: 600, margin: 0 } }, 'UsageDeck'),
          h(
            'p',
            { style: { ...MUTED, margin: '0.25rem 0 0' } },
            "Read-only view of the active gateway's detected provider subscriptions, this profile's session usage and self-hosted endpoint health. No credentials are requested."
          ),
          props.scopeLabel ? h('p', { 'data-scope-label': 'true', style: { ...MUTED, margin: '0.25rem 0 0', overflowWrap: 'anywhere' } }, props.scopeLabel) : null
        ),
        h(
          'div',
          { style: { display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '0.25rem' } },
          h(
            'div',
            { style: { display: 'flex', alignItems: 'center', gap: '0.5rem' } },
            props.updatedAt ? h('span', { style: MUTED }, 'Updated ' + fmtEpochMs(props.updatedAt)) : null,
            h(
              ui.Button,
              { type: 'button', variant: 'outline', size: 'sm', onClick: props.onRefresh, disabled: !props.onRefresh || props.busy },
              props.busy ? 'Refreshing…' : 'Refresh'
            )
          ),
          h('span', { style: { ...MUTED, maxWidth: '22rem', textAlign: 'right' } }, 'Refresh re-requests all three zones. The backend may answer from its 30-second cache, so values can be up to 30 seconds old.')
        )
      ),
      props.children,
      h('div', { role: 'status', 'aria-live': 'polite', style: SR_ONLY }, props.busy ? 'Refreshing UsageDeck data' : 'UsageDeck data up to date')
    )
  )
}

function Notice(props) {
  return h(
    'div',
    { role: 'status', 'data-usage-deck-notice': props.kind, style: { marginTop: '2rem' } },
    h(ui.EmptyState, { title: props.title, description: props.description })
  )
}

function connectionLabel(scope) {
  return (
    'Connection: ' +
    (scope.connectionKey === 'ambient' ? 'active gateway (ambient)' : scope.connectionKey) +
    ' · Profile: ' +
    (scope.profileKey || 'default')
  )
}

// Owns every resource observer and the provider control for ONE scope tuple. The parent
// keys it by the tuple, so a scope change remounts it with fresh loading state.
function ScopedPage(props) {
  const reg = props.reg
  const scope = props.scope
  const view = props.view
  const windowDays = props.windowDays
  const dimension = effectiveDimension(view, props.dimension)
  const [provider, setProvider] = useState('')
  const live = scopeIsLive(reg, scope)

  const subsQ = useResourceQuery(reg, scope, {
    resource: 'subscriptions',
    view,
    windowKey: null,
    providerKey: '',
    path: '/subscriptions?view=' + view,
    validate: validateSubscriptions,
    enabled: live
  })
  const usageQ = useResourceQuery(reg, scope, {
    resource: 'usage',
    view,
    windowKey: windowDays,
    providerKey: provider,
    dimension,
    version: USAGE_SCHEMA_VERSION,
    path: '/usage?mode=' + view + '&dimension=' + dimension + '&window=' + windowDays + (provider ? '&provider=' + encodeURIComponent(provider) : ''),
    validate: env => validateUsage(env, windowDays, { provider, mode: view, dimension }),
    enabled: live
  })
  const healthQ = useResourceQuery(reg, scope, {
    resource: 'health',
    view,
    windowKey: null,
    providerKey: '',
    path: '/health?view=' + view,
    validate: validateHealth,
    enabled: live
  })

  // No cached DTO is exposed unless the live identity still equals this page's scope.
  const subs = resourceState(live ? subsQ : null)
  const usage = resourceState(live ? usageQ : null)
  const health = resourceState(live ? healthQ : null)
  const effectiveView = subs.data ? subs.data.view : null

  // Provider options come from the analytics itself (historical providers + Unknown), not the subscription
  // inventory; the last answer's list is kept while the next one loads so the selector never flickers empty.
  const [providerOptions, setProviderOptions] = useState([])
  const usageData = usage.data

  useEffect(() => {
    if (usageData) {
      setProviderOptions(usageData.providers)
    }
  }, [usageData])

  const selectable = useMemo(
    () => (provider && !providerOptions.some(p => p.id === provider) ? [...providerOptions, { id: provider, label: provider }] : providerOptions),
    [providerOptions, provider]
  )

  // provider_not_found invalidates the filter (not a missing backend).
  const usageErr = usage.error
  const providerGone = !!provider && !!usageErr && usageErr.kind === 'deck' && usageErr.code === 'provider_not_found'

  useEffect(() => {
    if (providerGone) {
      setProvider('')
    }
  }, [providerGone])

  const retrySubs = useCallback(() => {
    if (scopeIsLive(reg, scope)) {
      subsQ.refetch()
    }
  }, [reg, scope, subsQ])
  const retryUsage = useCallback(() => {
    if (scopeIsLive(reg, scope)) {
      usageQ.refetch()
    }
  }, [reg, scope, usageQ])
  const retryHealth = useCallback(() => {
    if (scopeIsLive(reg, scope)) {
      healthQ.refetch()
    }
  }, [reg, scope, healthQ])
  const refreshAll = useCallback(() => {
    if (!scopeIsLive(reg, scope)) {
      return
    }

    subsQ.refetch()
    usageQ.refetch()
    healthQ.refetch()
  }, [reg, scope, subsQ, usageQ, healthQ])

  const busy = live && (subsQ.isFetching || usageQ.isFetching || healthQ.isFetching)
  const updatedAt = live ? Math.max(subsQ.dataUpdatedAt || 0, usageQ.dataUpdatedAt || 0, healthQ.dataUpdatedAt || 0) : 0

  return h(
    PageShell,
    { scopeLabel: connectionLabel(scope), updatedAt, busy, onRefresh: live ? refreshAll : null },
    h(SubscriptionsZone, { rs: subs, view, onView: props.onView, onRetry: retrySubs }),
    h(UsageZone, {
      rs: usage,
      windowDays,
      onWindow: props.onWindow,
      provider,
      onProvider: setProvider,
      providers: selectable,
      view,
      dimension,
      onDimension: props.onDimension,
      onRetry: retryUsage
    }),
    h(HealthZone, { rs: health, effectiveView, requestedView: view, onRetry: retryHealth })
  )
}

// Reads the active gateway identity reactively and mounts the scope-keyed page.
function GatedPage(props) {
  const reg = props.reg
  const st = hostState()
  const gateway = ui.useValue(st.gateway)
  const profile = ui.useValue(st.profile)
  const connectionId = ui.useValue(st.connectionId)
  const epoch = ui.useGatewayActivationEpoch()
  const [view, setView] = useState(DEFAULT_VIEW)
  const [windowDays, setWindowDays] = useState(DEFAULT_WINDOW)
  const [dimension, setDimension] = useState(DEFAULT_DIMENSION)
  // Machine -> profile resets a by-profile dimension to All before anything is fetched.
  const onView = v => {
    setView(v)
    setDimension(d => effectiveDimension(v, d))
  }
  const scope = {
    generation: reg.id,
    epoch,
    connectionKey: connectionKeyOf(connectionId),
    profileKey: profileKeyOf(profile)
  }
  const open = gateway === 'open'

  // Retire other scopes' rows (all rows while the gateway is not open).
  useEffect(() => {
    purge(reg, open ? scope : null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reg, open, epoch, scope.connectionKey, scope.profileKey])

  if (reg.disposed) {
    return null
  }

  if (!open) {
    const connecting = gateway === 'connecting'

    return h(
      PageShell,
      { scopeLabel: null, updatedAt: 0, busy: false, onRefresh: null },
      h(Notice, {
        kind: connecting ? 'connecting' : 'no-gateway',
        title: connecting ? NOTICE_CONNECTING : NOTICE_CONNECT,
        description: 'UsageDeck reads the active gateway only. Nothing is requested until a gateway is open.'
      })
    )
  }

  return h(ScopedPage, {
    key: [scope.generation, scope.epoch, scope.connectionKey, scope.profileKey].join('\u0000'),
    reg,
    scope,
    view,
    windowDays,
    dimension,
    onView,
    onWindow: setWindowDays,
    onDimension: setDimension
  })
}

// Entry point of the registered page. The identity check is constant for a given desktop
// build, so the hook-bearing child is either always or never mounted (§4.5).
function UsagePage(props) {
  if (!identityReady()) {
    return h(
      PageShell,
      { scopeLabel: null, updatedAt: 0, busy: false, onRefresh: null },
      h(Notice, {
        kind: 'update-desktop',
        title: NOTICE_UPDATE,
        description: 'This Hermes Desktop build lacks the gateway identity APIs UsageDeck needs, so it makes no requests.'
      })
    )
  }

  return h(GatedPage, { reg: props.reg })
}

// ---------------------------------------------------------------------------
// Plugin
// ---------------------------------------------------------------------------

function subscribeAtom(atom, fn) {
  if (!isAtom(atom)) {
    return null
  }

  return typeof atom.listen === 'function' ? atom.listen(fn) : typeof atom.subscribe === 'function' ? atom.subscribe(fn) : null
}

export default {
  id: PLUGIN_ID,
  name: 'UsageDeck',
  defaultEnabled: false,
  register(ctx) {
    const reg = { id: newGeneration(), ctx, disposed: false }

    // Retire this registration's requests: mark disposed first, then cancel and drop only
    // this generation's cache rows (never the app's whole QueryClient).
    ctx.onDispose(() => {
      reg.disposed = true

      try {
        const qc = ui.queryClient

        if (qc) {
          const pending = qc.cancelQueries({ queryKey: [QUERY_ROOT, reg.id] })

          if (pending && typeof pending.catch === 'function') {
            pending.catch(() => {})
          }

          qc.removeQueries({ queryKey: [QUERY_ROOT, reg.id] })
        }
      } catch {
        // disposal is best effort; late results are still rejected by the scope guard
      }
    })

    // Plugin-owned store subscriptions: drop retired scopes even while the page is unmounted.
    // The epoch has no non-React subscription, so it is covered by the keyed render and the
    // pre/post-await guards instead.
    if (identityReady()) {
      const st = hostState()
      const sweep = () => {
        if (reg.disposed || !identityReady()) {
          return
        }

        const live = readLive()

        purge(reg, live.gateway === 'open' ? live : null)
      }

      ;[st.gateway, st.profile, st.connectionId].forEach(atom => {
        const stop = subscribeAtom(atom, sweep)

        if (typeof stop === 'function') {
          ctx.onDispose(stop)
        }
      })
    }

    ctx.register({
      id: 'page',
      area: ui.ROUTES_AREA,
      data: { path: ROUTE_PATH },
      render: () => jsx(UsagePage, { reg })
    })
    ctx.register({
      id: 'nav',
      area: ui.SIDEBAR_NAV_AREA,
      data: { path: ROUTE_PATH, label: 'UsageDeck', codicon: 'graph' }
    })
  }
}
