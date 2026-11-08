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

const CONF_LABEL = { exact: 'Exact', estimated: 'Estimated', opaque: 'Opaque', self_hosted: 'Self-hosted' }
const CONF_RANK = { opaque: 0, estimated: 1, exact: 2 }
// Highest confidence a given data source can honestly carry (DESIGN-desktop §5.3).
const SOURCE_CEILING = {
  openrouter_key: 'exact',
  session_analytics: 'estimated',
  prometheus: 'self_hosted',
  anthropic_headers: 'exact',
  chatgpt_wham_usage: 'exact',
  none: 'opaque'
}
const SOURCE_LABEL = {
  openrouter_key: 'OpenRouter key endpoint',
  session_analytics: 'Hermes session analytics',
  prometheus: 'Endpoint metrics',
  anthropic_headers: 'Anthropic unified rate-limit headers captured from the last call',
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
    'No Anthropic session/weekly meter: no usable capture for this credential. Enable the optional observer and use this account normally; a meter can only appear after an ordinary request returns eligible headers. Nothing is estimated.'
}
const MACHINE_ONLY_NOTE =
  "No live source for this account: the machine view reads meters with any local profile's credential only through the documented read-only usage probes (Codex spend control, endpoint /metrics), and none exists for this account, so it is local inventory. Nothing is estimated."

// Fixed local copy for Reason codes: upstream text never reaches the UI.
const REASON_TEXT = {
  unsupported: 'No verified usage source exists for this provider.',
  credentials_missing: 'No credential was sent for this account (none was found).',
  permission_denied: 'The available credential does not permit reading this data.',
  attribution_unavailable: 'Provider attribution unavailable from session analytics.',
  no_observations: 'No observations for this period.',
  metrics_unavailable: 'This endpoint does not expose supported metrics.',
  source_unavailable: 'The data source is currently unavailable.'
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

  if (card.provider_id === 'anthropic' && isObj(card.debug) && card.debug.source === 'anthropic_headers') {
    return null // had a capture
  }

  return holderNames(card).length > 0 ? MACHINE_ONLY_NOTE : null
}

// Explanation for an opaque card of a provider that could expose usage, or null.
function opaqueNote(card) {
  if (!isObj(card) || card.kind === 'self_hosted' || cardView(card) !== 'unavailable') {
    return null
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

function unwrapEnvelope(resp) {
  if (!isObj(resp)) {
    throw new UdError('malformed')
  }

  if (resp.ok === false) {
    const code = knownDeckCode(resp)

    throw code ? new UdError('deck', { code }) : new UdError('malformed')
  }

  if (resp.ok !== true || !isObj(resp.meta) || resp.meta.schema_version !== 1 || !isObj(resp.data)) {
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

function validateUsage(env, windowDays, provider) {
  const d = env.data

  if (d.window_days !== windowDays || !Array.isArray(d.series) || !d.series.every(isObj)) {
    throw new UdError('malformed')
  }

  if ((d.provider_id ?? null) !== (provider || null)) {
    throw new UdError('malformed')
  }

  return {
    window_days: d.window_days,
    provider_id: str(d.provider_id),
    series: d.series,
    reason: has(REASON_TEXT, d.reason) ? d.reason : null,
    meta: env.meta
  }
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
async function guardedGet(reg, scope, path, validate) {
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

  return validate(unwrapEnvelope(raw))
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
    spec.providerKey
  ]

  return ui.useQuery({
    queryKey: key,
    queryFn: () => guardedGet(reg, scope, spec.path, spec.validate),
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
    { 'aria-labelledby': headingId, 'data-zone': props.id, style: { marginTop: '1.75rem' } },
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
  const fromHeaders = m.source === 'anthropic_headers'
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
    (fromHeaders ? ' Value as of that call, not live; it is never extrapolated.' : '') +
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

function SeriesBlock(props) {
  const s = props.series
  const conf = effectiveConfidence(s.confidence, has(SOURCE_CEILING, s.source) ? SOURCE_CEILING[s.source] : 'opaque')
  const sorted = sortedByDay(s.points)
  const tip =
    'Source: Hermes session analytics. Scope: ' +
    (s.scope === 'profile' ? 'this Hermes profile' : 'provider-attributed') +
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
      h(ui.Badge, { variant: 'outline' }, s.scope === 'profile' ? 'This Hermes profile' : 'Provider-attributed'),
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
              h('thead', null, h('tr', null, ['Day', 'Input', 'Output', 'Est. USD', 'Sessions', 'API calls'].map(th))),
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
                    td('s', fmtNumber(p.sessions, 'count')),
                    td('a', fmtNumber(p.api_calls, 'count'))
                  )
                )
              )
            )
          )
        )
  )
}

function UsageZone(props) {
  const rs = props.rs
  const providers = props.providers
  const selected = providers.filter(p => p.id === props.provider)[0]
  let content

  if (rs.phase === 'loading') {
    content = h(LoadingBlock, { rows: 5, label: 'Loading usage analytics…' })
  } else if (rs.phase === 'error') {
    content = h(ErrorBlock, { what: 'usage analytics', error: rs.error, onRetry: props.onRetry })
  } else {
    const d = rs.data
    const err = rs.error ? h(ErrorBlock, { key: 'err', what: 'usage analytics', error: rs.error, onRetry: props.onRetry }) : null
    let inner

    if (d.series.length === 0) {
      const reasonKey = d.reason || (props.provider ? 'attribution_unavailable' : 'no_observations')

      inner = h('p', { 'data-chart-state': reasonKey, style: { ...SMALL, margin: 0 } }, REASON_TEXT[reasonKey])
    } else {
      inner = d.series.map((s, i) => h(SeriesBlock, { key: str(s.id) || i, series: s }))
    }

    content = h('div', { 'aria-busy': rs.refreshing ? 'true' : undefined }, err, inner)
  }

  return h(
    Zone,
    {
      id: 'usage',
      title: 'Usage analytics',
      badge: zoneBadge(rs),
      controls: [
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
            h(ui.SelectItem, { key: ALL_PROVIDERS, value: ALL_PROVIDERS }, 'All (this profile)'),
            providers.map(p => h(ui.SelectItem, { key: p.id, value: p.id }, p.label))
          )
        )
      ],
      note:
        'This Hermes profile only — not subscription allowance. Usage follows the active gateway profile even when machine inventory is selected.' +
        (props.provider ? ' Filtered to ' + (selected ? selected.label : 'the selected provider') + '. Session analytics carry no per-provider attribution.' : '')
    },
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
    props.machine && statusKey === 'unknown' && e.reason === 'source_unavailable' && !errCode
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
    path: '/usage?window=' + windowDays + '&view=' + view + (provider ? '&provider=' + encodeURIComponent(provider) : ''),
    validate: env => validateUsage(env, windowDays, provider),
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

  const providers = useMemo(() => {
    const seen = {}
    const out = []
    const cards = subs.data ? subs.data.cards : []

    cards.forEach(c => {
      const id = str(c.provider_id)

      if (id && !has(seen, id)) {
        seen[id] = true
        out.push({ id, label: str(c.provider_label) || id })
      }
    })

    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subs.data])

  const selectable = useMemo(
    () => (provider && !providers.some(p => p.id === provider) ? [...providers, { id: provider, label: provider }] : providers),
    [providers, provider]
  )

  // A provider that vanished from a successfully loaded inventory falls back to All.
  useEffect(() => {
    if (provider && subs.phase === 'ready' && !subs.error && !providers.some(p => p.id === provider)) {
      setProvider('')
    }
  }, [provider, providers, subs.phase, subs.error])

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
    onView: setView,
    onWindow: setWindowDays
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
