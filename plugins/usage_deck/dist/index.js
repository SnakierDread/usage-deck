/**
 * UsageDeck — Hermes dashboard plugin (frontend).
 *
 * One screen, three independent zones:
 *   1. Subscriptions grid   GET /api/plugins/usage-deck/subscriptions?view=machine|profile
 *   2. Usage analytics      GET /api/plugins/usage-deck/usage?mode=machine|profile&dimension=all|profile|model|repo&window=7|30|90[&provider=<id>]
 *   3. Self-hosted health   GET /api/plugins/usage-deck/health?view=machine|profile
 *
 * Plain IIFE, no build step, no runtime dependencies (DESIGN.md §6): React, hooks and
 * fetchJSON come from window.__HERMES_PLUGIN_SDK__; charts are native SVG; every colour
 * is a host CSS custom property (see style.css). All server strings are rendered as
 * text nodes only — never innerHTML.
 *
 * Honesty rules enforced here (DESIGN.md §4–5):
 *  - null is never rendered as zero; opaque data is never rendered as a number or bar;
 *  - a confidence badge is never higher than the meter's source allows;
 *  - progress bars need a finite value >= 0, a finite limit > 0, non-opaque confidence
 *    and a known period; otherwise the number is shown without a bar;
 *  - there is no credential input anywhere in this plugin.
 */
(function (root) {
  "use strict";

  // ---------------------------------------------------------------------------
  // Constants
  // ---------------------------------------------------------------------------

  // Single source of truth for the API namespace (DESIGN.md §0). No alias, no origin.
  const API_BASE = "/api/plugins/usage-deck";
  const WINDOWS = [7, 30, 90];
  const DEFAULT_WINDOW = 30;
  // Subscriptions/health scope (DESIGN.md §10): the whole machine (default) or the selected profile.
  const VIEWS = ["machine", "profile"];
  const DEFAULT_VIEW = "machine";
  const POLL_MS = 60000;
  // Usage analytics dimension (analytics-dimensions-breakdown §7): how the scoped session records are partitioned.
  const DIMENSIONS = ["all", "profile", "model", "repo"];
  const DEFAULT_DIMENSION = "all";
  const USAGE_SCHEMA_VERSION = 2;
  const GROUP_PALETTE = 8; // ud-g0..ud-g7 colour classes (cycled)
  // Copy of the usage zone (fixed local text; upstream strings only ever appear as text nodes).
  const COPY_SCOPE_MACHINE = "All local profile session records — not subscription allowance.";
  const COPY_SCOPE_PROFILE = "This Hermes profile only — not subscription allowance.";
  const COPY_ATTRIBUTION_LIMIT = "Attribution follows each session's recorded model and provider (its last recorded values after a switch), counted once per session; it does not reconstruct every model call.";
  const ATTRIBUTION_TIP = {
    session_provider: "Provider recorded on the session.",
    billing_provider: "Provider taken from the session's billing provider; not measured billing ownership.",
    models_json: "Provider inferred from this profile's local models.json catalog by exact model id; not measured billing ownership.",
    unknown: "No provider was recorded for this session and the local catalog does not name one.",
  };

  const CONF_LABEL = {
    exact: "Exact",
    estimated: "Estimated",
    opaque: "Opaque",
    self_hosted: "Self-hosted",
  };
  const CONF_RANK = { opaque: 0, estimated: 1, exact: 2 };
  // Highest confidence a given data source can honestly carry.
  const SOURCE_CEILING = {
    openrouter_key: "exact",
    session_analytics: "estimated",
    prometheus: "self_hosted",
    anthropic_headers: "exact",
    anthropic_oauth_usage: "exact",
    chatgpt_wham_usage: "exact",
    none: "opaque",
  };
  const SOURCE_LABEL = {
    openrouter_key: "OpenRouter key endpoint",
    session_analytics: "Hermes session analytics",
    prometheus: "Endpoint metrics",
    anthropic_headers: "Anthropic unified rate-limit headers captured from the last call",
    anthropic_oauth_usage: "Anthropic OAuth usage endpoint (read live)",
    chatgpt_wham_usage: "ChatGPT workspace spend control (wham/usage)",
    none: "No verified source",
  };
  // Provider-specific, fixed local explanations for an opaque card that shows no meter (DESIGN.md §9, §12.1).
  // Codex: source-aware (§9.2). The superseded blanket "never read" explanation is gone: the route is
  // ratified and read, so an opaque card means this account returned nothing this meter can honestly show.
  const OPAQUE_NOTES = {
    "openai-codex": "No Codex spend meter for this account: UsageDeck reads only the ChatGPT workspace spend control, and this account has none it can show right now (no usable credential, an unrecognised or expired reading, or access was not permitted). Nothing is estimated. Any plan shown is an unverified claim from the credential.",
    anthropic: "No Anthropic session/weekly meter: no live usage reading for this credential (the live read needs a persisted OAuth login of the selected profile) and no usable capture; enable the optional observer and use this account normally. A captured meter can only appear after an ordinary request returns eligible headers (DESIGN.md §12.1). Nothing is estimated.",
  };
  // Anthropic card whose live usage endpoint answered but yielded no meter (DESIGN.md §9.1.2): the live answer is
  // definitive, so no older captured reading is shown in its place.
  const ANTHROPIC_LIVE_EMPTY_NOTE = "No Anthropic session/weekly meter: Anthropic's usage endpoint was read live for this credential and reported no current session or weekly window (or did not permit the read). Nothing is estimated and no older captured reading is shown in its place.";
  // Machine view only (DESIGN.md §12.5, amended by §13): the machine view reads meters with any local profile's
  // credential, but only through the documented read-only usage probes; other accounts stay local inventory.
  const MACHINE_ONLY_NOTE = "No live source for this account: the machine view reads meters with any local profile's credential only through the documented read-only usage probes (Codex spend control, Anthropic OAuth usage, endpoint /metrics and models listing), and none exists for this account, so it is local inventory. Nothing is estimated.";
  // Machine view, self-hosted health row of an endpoint owned by ANOTHER profile's inventory (config or credential pool; ownership, not proof of a request) (DESIGN.md §13 item 3b).
  const PROFILE_OWNED_BADGE = "Profile endpoint";
  const PROFILE_OWNED_NOTE = "This endpoint belongs to another profile's inventory (its config or its credential pool), not to the selected profile. The machine view's read-only probe (/metrics and the models listing) uses that profile's own address, anonymously unless that profile's own key_env supplies a key; when that key is unset, no request is sent. No other profile's or the process's credential is sent. The row's status shows the outcome.";
  const SCOPE_LABEL = {
    api_credential: "API credential",
    provider_account: "Provider account",
    endpoint: "Endpoint",
    profile: "This Hermes profile",
  };
  const UNIT_LABEL = { usd: "USD", count: "count", percent: "percent", bytes: "bytes", credit: "credits" };

  // Fixed local copy for Reason codes — upstream text never reaches the UI.
  const REASON_TEXT = {
    unsupported: "No verified usage source exists for this provider.",
    credentials_missing: "No credential was sent for this account (none was found).",
    permission_denied: "The available credential does not permit reading this data.",
    attribution_unavailable: "Provider attribution unavailable from session analytics.",
    no_observations: "No observations for this period.",
    metrics_unavailable: "This endpoint does not expose supported metrics.",
    source_unavailable: "The data source is currently unavailable.",
    box_offline: "Box offline: the endpoint is not reachable right now.",
  };
  /** "Last seen …" text of a self-hosted card or health entry (server-process memory, null = never seen). */
  function lastSeenText(obj) {
    if (!isObj(obj)) return null;
    const seen = str(obj.last_seen_at);
    return seen ? "Last seen " + fmtTime(seen) : "Not seen since the server started";
  }
  const KNOWN_ERROR_CODES = {
    invalid_query: 1, provider_not_found: 1, auth_required: 1, permission_denied: 1,
    timeout: 1, upstream_unavailable: 1, malformed_response: 1, inventory_unavailable: 1,
    analytics_unavailable: 1, internal_error: 1,
  };
  // Credential state of a card. Three distinct truths, never conflated: a credential was found
  // (presence only), none was found so none was sent, or one was sent and the provider refused it.
  const AUTH_INFO = {
    available: { text: "Credential present (not validated)",
      tip: "A credential was found and is used for requests to this provider. It has not been confirmed to work." },
    missing: { text: "No credential found",
      tip: "No credential was found, so none was sent. A provider that answers 401 to an unauthenticated request does not mean the credential is wrong." },
    rejected: { text: "Credential rejected",
      tip: "A credential was sent and the provider answered 401 (unauthorized). The stored credential is probably wrong or expired." },
  };
  function authInfo(status) {
    const key = Object.prototype.hasOwnProperty.call(AUTH_INFO, status) ? status : "missing";
    return { key: key, text: AUTH_INFO[key].text, tip: AUTH_INFO[key].tip };
  }
  const CARD_STATES = { ready: 1, error: 1, opaque: 1, empty: 1 };
  const HEALTH_META = {
    healthy: { icon: "●", text: "Healthy" },
    degraded: { icon: "▲", text: "Degraded" },
    offline: { icon: "✕", text: "Offline" },
    unknown: { icon: "?", text: "Unknown" },
  };

  // ---------------------------------------------------------------------------
  // Pure helpers (exported for tests)
  // ---------------------------------------------------------------------------

  function isObj(v) {
    return v !== null && typeof v === "object" && !Array.isArray(v);
  }
  function num(v) {
    return typeof v === "number" && isFinite(v) ? v : null;
  }
  function str(v) {
    return typeof v === "string" ? v : null;
  }
  function has(map, key) {
    return typeof key === "string" && Object.prototype.hasOwnProperty.call(map, key);
  }

  function asConfidence(v) {
    return has(CONF_LABEL, v) ? v : "opaque";
  }

  /**
   * Lower of a claimed confidence and the ceiling its source allows. Exact/estimated are
   * ordered tiers; self_hosted is a source category, so any cross-category mismatch
   * collapses to opaque rather than guessing.
   */
  function effectiveConfidence(own, ceiling) {
    const a = asConfidence(own);
    const c = asConfidence(ceiling);
    if (a === c) return a;
    if (a === "opaque" || c === "opaque") return "opaque";
    if (a === "self_hosted" || c === "self_hosted") return "opaque";
    return CONF_RANK[a] <= CONF_RANK[c] ? a : c;
  }

  function meterConfidence(m) {
    const ceiling = has(SOURCE_CEILING, m && m.source) ? SOURCE_CEILING[m.source] : "opaque";
    return effectiveConfidence(m && m.confidence, ceiling);
  }

  /** A meter carries at least one finite number and is not opaque. */
  function isMeasured(m) {
    if (!isObj(m)) return false;
    if (meterConfidence(m) === "opaque") return false;
    return num(m.value) !== null || num(m.limit) !== null || num(m.remaining) !== null;
  }

  /**
   * Progress-bar eligibility (DESIGN.md §5): finite value >= 0, finite limit > 0,
   * non-opaque confidence and a known period. Returns the raw ratio (uncapped) or null.
   */
  function progressRatio(m) {
    if (!isMeasured(m)) return null;
    const value = num(m.value);
    const limit = num(m.limit);
    if (value === null || limit === null || value < 0 || limit <= 0) return null;
    const label = isObj(m.period) ? str(m.period.label) : null;
    if (!label || label === "unknown") return null;
    return value / limit;
  }

  /** Card-level badge: never above what the card's measured meters can support. */
  function cardConfidence(card) {
    const own = asConfidence(card && card.confidence);
    if (!card || card.state === "opaque" || card.state === "error") return "opaque";
    const measured = (Array.isArray(card.meters) ? card.meters : []).filter(isMeasured);
    if (card.state === "ready" && measured.length === 0) return "opaque";
    if (own === "opaque") return "opaque";
    if (own === "self_hosted") {
      return measured.some(function (m) { return meterConfidence(m) === "self_hosted"; })
        ? "self_hosted" : "opaque";
    }
    if (measured.length === 0) return own; // empty state: supported telemetry, no observations
    let best = "opaque";
    measured.forEach(function (m) {
      const c = meterConfidence(m);
      if (has(CONF_RANK, c) && CONF_RANK[c] > CONF_RANK[best]) best = c;
    });
    return CONF_RANK[own] <= CONF_RANK[best] ? own : best;
  }

  /**
   * What the card body shows: error | meters | empty | unavailable. A "ready" card with no
   * measured value is displayed as unavailable — never as a zero.
   */
  function cardView(card) {
    if (!isObj(card)) return "unavailable";
    if (card.state === "error" || (card.state !== "ready" && card.state !== "opaque" && card.state !== "empty")) {
      return "error";
    }
    const measured = (Array.isArray(card.meters) ? card.meters : []).filter(isMeasured);
    if (measured.length > 0) return "meters";
    if (card.state === "empty") return "empty";
    return "unavailable";
  }

  function fmtNumber(n, unit) {
    const v = num(n);
    if (v === null) return "—";
    if (unit === "usd") {
      const abs = Math.abs(v);
      return (v < 0 ? "-" : "") + "$" + abs.toLocaleString("en-US", {
        minimumFractionDigits: abs >= 1 ? 2 : 2,
        maximumFractionDigits: abs >= 1 ? 2 : 4,
      });
    }
    if (unit === "bytes") {
      const units = ["B", "KiB", "MiB", "GiB", "TiB"];
      let x = Math.abs(v);
      let i = 0;
      while (x >= 1024 && i < units.length - 1) { x /= 1024; i++; }
      return (v < 0 ? "-" : "") + (i === 0 ? String(Math.round(x)) : x.toFixed(1)) + " " + units[i];
    }
    if (unit === "percent") return (Math.round(v * 10) / 10) + "%";
    if (unit === "credit") return v.toLocaleString("en-US", { maximumFractionDigits: 2 }) + " credits";
    return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }

  /** "2026-11-06T12:30:00Z" -> "2026-11-06 12:30 UTC" (deterministic, unambiguous). */
  function fmtTime(iso) {
    const s = str(iso);
    if (!s) return "unknown time";
    const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(s);
    return m ? m[1] + " " + m[2] + " UTC" : "unknown time";
  }

  function dayToMs(day) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(typeof day === "string" ? day : "");
    if (!m) return null;
    const t = Date.UTC(+m[1], +m[2] - 1, +m[3]);
    return isFinite(t) ? t : null;
  }

  /**
   * Split a day-ascending series into contiguous runs for one field. A run breaks at a
   * null value and at a missing calendar day, so the chart never bridges gaps or plots
   * unobserved days as zero.
   */
  function splitSegments(points, field) {
    const segments = [];
    let cur = null;
    let prevT = null;
    (Array.isArray(points) ? points : []).forEach(function (p) {
      const t = dayToMs(p && p.day);
      const v = p ? num(p[field]) : null;
      if (t === null || v === null) { cur = null; prevT = null; return; }
      if (cur && prevT !== null && t - prevT === 86400000) {
        cur.push({ day: p.day, t: t, v: v });
      } else {
        cur = [{ day: p.day, t: t, v: v }];
        segments.push(cur);
      }
      prevT = t;
    });
    return segments;
  }

  function niceMax(max) {
    if (!(max > 0)) return 1;
    const mag = Math.pow(10, Math.floor(Math.log10(max)));
    const steps = [1, 2, 2.5, 5, 10];
    for (let i = 0; i < steps.length; i++) {
      if (max <= steps[i] * mag) return steps[i] * mag;
    }
    return 10 * mag;
  }

  function sortedByDay(points) {
    return (Array.isArray(points) ? points : []).filter(function (p) {
      return isObj(p) && dayToMs(p.day) !== null;
    }).sort(function (a, b) { return dayToMs(a.day) - dayToMs(b.day); });
  }

  /** Classify a thrown fetchJSON error / envelope problem into a UI error. */
  function classifyError(err) {
    if (err && err.uiError) return err.uiError;
    const raw = err && err.message ? String(err.message) : "";
    const m = /^(\d{3}):\s*([\s\S]*)$/.exec(raw);
    const status = m ? parseInt(m[1], 10) : null;
    const body = m ? m[2] : raw;
    if (status === 401 || status === 403) {
      return {
        kind: "access", status: status, code: null, retryable: false,
        message: "The dashboard denied this request. Sign in to the dashboard again or check this profile's access. UsageDeck never asks for provider credentials.",
      };
    }
    let parsed = null;
    try { parsed = JSON.parse(body); } catch (_e) { parsed = null; }
    const de = deckErrorFrom(parsed);
    if (de) return Object.assign({ kind: "deck", status: status }, de);
    if (status !== null) {
      return { kind: "transport", status: status, code: null, retryable: true, message: "UsageDeck API request failed (HTTP " + status + ")." };
    }
    return { kind: "transport", status: null, code: null, retryable: true, message: "Could not reach the UsageDeck API." };
  }

  // Accepts {ok:false,error:DeckError}, {error:DeckError} or FastAPI {detail:{error|code,...}}.
  function deckErrorFrom(obj) {
    if (!isObj(obj)) return null;
    let e = obj.error;
    if (!isObj(e) && isObj(obj.detail)) e = isObj(obj.detail.error) ? obj.detail.error : obj.detail;
    if (!isObj(e) || !has(KNOWN_ERROR_CODES, e.code)) return null;
    return {
      code: e.code,
      retryable: e.retryable === true,
      message: typeof e.message === "string" && e.message ? e.message.slice(0, 300) : "The request could not be completed.",
    };
  }

  function malformed(why) {
    return {
      uiError: {
        kind: "malformed", status: null, code: "malformed_response", retryable: true,
        message: "Unexpected response from the UsageDeck API" + (why ? " (" + why + ")" : "") + ".",
      },
    };
  }

  /** Validate the envelope (DESIGN.md §3) and return `data`, or throw {uiError}. `version` is the wire version
   *  the endpoint speaks: 1 for subscriptions/health, 2 for usage (dimensions payload). */
  function unwrapEnvelope(resp, version) {
    if (!isObj(resp)) throw malformed("not an object");
    if (resp.ok === false) {
      const de = deckErrorFrom(resp);
      if (de) throw { uiError: Object.assign({ kind: "deck", status: null }, de) };
      throw malformed("failure without error");
    }
    if (resp.ok !== true) throw malformed("missing ok flag");
    if (!isObj(resp.meta) || resp.meta.schema_version !== (version || 1)) throw malformed("unsupported schema version");
    if (!isObj(resp.data)) throw malformed("missing data");
    return { data: resp.data, meta: resp.meta };
  }

  function validateSubscriptions(env) {
    const d = env.data;
    if (!Array.isArray(d.cards)) throw malformed("cards");
    const cards = d.cards.filter(isObj).map(function (c) {
      return c;
    });
    if (cards.length !== d.cards.length) throw malformed("card entries");
    return {
      cards: cards, inventory_state: d.inventory_state === "empty" ? "empty" : "detected",
      view: d.view === "profile" ? "profile" : "machine", meta: env.meta,
    };
  }

  const USAGE_REASONS = { no_observations: 1, source_unavailable: 1 };
  const METRIC_FIELDS = ["input_count", "output_count", "estimated_usd", "sessions", "api_calls"];

  function validMetrics(m) {
    if (!isObj(m) || !Array.isArray(m.missing_fields)) return false;
    return METRIC_FIELDS.every(function (f) { return m[f] === null || num(m[f]) !== null; });
  }

  /**
   * Validate the v2 usage payload (analytics-dimensions-breakdown §4). `query` is {provider, mode, dimension}
   * as requested: the response must be for the same window and the same provider filter (a stale or unfiltered
   * answer is never shown under a filtered label). mode/dimension are taken from the response: the server may
   * clamp them (profile-restricted principal) and the UI renders the EFFECTIVE scope.
   */
  function validateUsage(env, windowDays, query) {
    const d = env.data;
    const q = query || {};
    if (d.window_days !== windowDays) throw malformed("window mismatch");
    if ((str(d.provider_id) || null) !== (q.provider || null)) throw malformed("provider mismatch");
    if (!has({ machine: 1, profile: 1 }, d.mode) || DIMENSIONS.indexOf(d.dimension) < 0) throw malformed("scope");
    if (q.mode && q.dimension && d.mode === q.mode && d.dimension !== q.dimension) throw malformed("dimension mismatch");
    ["series", "groups", "rows", "providers", "sources"].forEach(function (k) {
      if (!Array.isArray(d[k]) || !d[k].every(isObj)) throw malformed(k);
    });
    if (!validMetrics(d.totals)) throw malformed("totals");
    if (!d.groups.every(function (g) { return typeof g.id === "string" && typeof g.series_id === "string" && Array.isArray(g.row_ids) && validMetrics(g.totals); })) {
      throw malformed("groups");
    }
    if (!d.rows.every(function (r) { return typeof r.id === "string" && validMetrics(r.metrics); })) throw malformed("rows");
    return {
      window_days: d.window_days,
      provider_id: str(d.provider_id),
      mode: d.mode,
      dimension: d.dimension,
      series: d.series,
      groups: d.groups,
      rows: d.rows,
      totals: d.totals,
      providers: d.providers.filter(function (p) { return str(p.id) && str(p.label); }).map(function (p) { return { id: p.id, label: p.label }; }),
      sources: d.sources,
      complete: d.complete !== false,
      top_n: num(d.top_n),
      reason: has(USAGE_REASONS, d.reason) ? d.reason : null,
      pricing: isObj(d.pricing) ? d.pricing : null,
      depth: decodeDepth(d),
      meta: env.meta,
    };
  }

  /** Dimension options of a mode: machine offers All | By profile | By model | By repo, a single profile All | By model | By repo. */
  function dimensionOptions(mode) {
    const all = [{ id: "all", label: "All" }, { id: "profile", label: "By profile" }, { id: "model", label: "By model" }, { id: "repo", label: "By repo" }];
    return mode === "profile" ? all.filter(function (o) { return o.id !== "profile"; }) : all;
  }

  /** Dimension actually requested: a by-profile view does not exist for a single profile. */
  function effectiveDimension(mode, dimension) {
    return mode === "profile" && dimension === "profile" ? "all" : dimension;
  }

  /** Total tokens of a metrics object: null when neither component is known; `partial` when only one is. */
  function totalTokens(m) {
    const i = num(m && m.input_count), o = num(m && m.output_count);
    if (i === null && o === null) return { value: null, partial: false };
    return { value: (i || 0) + (o || 0), partial: i === null || o === null };
  }

  /** Cell text of a (possibly unavailable) count: null is a dash, a measured zero is 0. */
  function fmtMetric(v, unit) {
    return num(v) === null ? "—" : fmtNumber(v, unit || "count");
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
    return mode === "machine" ? COPY_SCOPE_MACHINE : COPY_SCOPE_PROFILE;
  }

  function providerLabelOf(providers, id) {
    const hit = (providers || []).filter(function (p) { return p.id === id; })[0];
    return hit ? hit.label : (str(id) || "Unknown");
  }

  function profileDbLabel(row) {
    return (row.is_global === true ? "global (default home)" : str(row.profile) || "profile") + " · state.db";
  }

  /** Groups joined with their series (by series_id), in server order; a group without a series is dropped. */
  function groupViews(data) {
    const byId = {};
    data.series.forEach(function (s) { if (str(s.id)) byId[s.id] = s; });
    const rowsById = {};
    data.rows.forEach(function (r) { rowsById[r.id] = r; });
    const out = [];
    data.groups.forEach(function (g, i) {
      const s = has(byId, g.series_id) ? byId[g.series_id] : null;
      if (!s) return;
      out.push({
        group: g, series: s, color: i % GROUP_PALETTE,
        rows: g.row_ids.map(function (id) { return rowsById[id]; }).filter(Boolean),
      });
    });
    return out;
  }

  function validateHealth(env) {
    if (!Array.isArray(env.data.entries)) throw malformed("entries");
    return { entries: env.data.entries.filter(isObj), meta: env.meta };
  }

  /** Holder profile names of a machine-view card, in server order; never throws on odd data. */
  function holderNames(card) {
    const list = card && Array.isArray(card.holders) ? card.holders : [];
    return list.filter(isObj).map(function (h) {
      const name = str(h.profile);
      return name ? (h.is_global === true ? name + " (root)" : name) : null;
    }).filter(Boolean);
  }

  /** Why a machine-view card has no live source (held only by other profiles), or null. */
  function machineOnlyNote(card) {
    if (!isObj(card) || cardView(card) !== "unavailable" || card.reason !== "source_unavailable") return null;
    if (card.provider_id === "anthropic" && isObj(card.debug) && (card.debug.source === "anthropic_headers" || card.debug.source === "anthropic_oauth_usage")) return null; // had a source
    return holderNames(card).length > 0 ? MACHINE_ONLY_NOTE : null;
  }

  /** Explanation for an opaque card of a provider that could expose usage, or null. */
  function opaqueNote(card) {
    if (!isObj(card) || card.kind === "self_hosted") return null;
    if (cardView(card) !== "unavailable") return null;
    if (card.provider_id === "anthropic" && isObj(card.debug) && card.debug.source === "anthropic_oauth_usage") {
      return ANTHROPIC_LIVE_EMPTY_NOTE;
    }
    return has(OPAQUE_NOTES, card.provider_id) ? OPAQUE_NOTES[card.provider_id] : null;
  }

  function sanitizeDomId(s) {
    return String(s).replace(/[^A-Za-z0-9_-]/g, "_");
  }

  // ---------------------------------------------------------------------------
  // Components
  // ---------------------------------------------------------------------------

  function createUsageDeck(SDK, options) {
    const React = SDK.React;
    const h = React.createElement;
    const useState = SDK.hooks.useState;
    const useEffect = SDK.hooks.useEffect;
    const useCallback = SDK.hooks.useCallback;
    const useMemo = SDK.hooks.useMemo;
    const useRef = SDK.hooks.useRef;
    const pollMs = (options && options.pollMs) || POLL_MS;

    function cx() {
      return Array.prototype.filter.call(arguments, Boolean).join(" ");
    }

    function getJSON(path, signal, version) {
      const init = signal ? { signal: signal } : undefined;
      return SDK.fetchJSON(API_BASE + path, init).then(function (resp) { return unwrapEnvelope(resp, version); });
    }

    function currentProfileKey() {
      try {
        const api = SDK.api;
        if (api && typeof api.getManagementProfile === "function") return String(api.getManagementProfile() || "");
      } catch (_e) { /* optional host helper */ }
      return "";
    }

    // --- resource hook: epoch-guarded fetch, stale-while-refresh, visible-only polling ---

    function useResource(key, load) {
      const [st, setSt] = useState({ key: key, status: "loading", data: null, error: null, loadedAt: 0, refreshing: false });
      const epoch = useRef(0);
      const loadRef = useRef(load);
      loadRef.current = load;
      const keyRef = useRef(key);
      keyRef.current = key;
      const ctrl = useRef(null);
      const inflight = useRef(false);
      const loadedAt = useRef(0);

      const run = useCallback(function (reset) {
        const my = ++epoch.current;
        const k = keyRef.current;
        if (ctrl.current) { try { ctrl.current.abort(); } catch (_e) { /* ignore */ } }
        const ac = typeof AbortController !== "undefined" ? new AbortController() : null;
        ctrl.current = ac;
        inflight.current = true;
        setSt(function (prev) {
          if (reset || prev.key !== k) {
            return { key: k, status: "loading", data: null, error: null, loadedAt: 0, refreshing: false };
          }
          return Object.assign({}, prev, { refreshing: true });
        });
        let p;
        try { p = Promise.resolve(loadRef.current(ac ? ac.signal : undefined)); } catch (e) { p = Promise.reject(e); }
        p.then(function (data) {
          if (my !== epoch.current) return; // late / superseded response: discard
          inflight.current = false;
          loadedAt.current = Date.now();
          setSt({ key: k, status: "ready", data: data, error: null, loadedAt: loadedAt.current, refreshing: false });
        }, function (e) {
          if (my !== epoch.current) return;
          inflight.current = false;
          setSt(function (prev) {
            // keep prior same-key data, visibly stale; never carry another key's data
            return {
              key: k, status: "error", data: prev.key === k ? prev.data : null,
              error: classifyError(e), loadedAt: prev.key === k ? prev.loadedAt : 0, refreshing: false,
            };
          });
        });
      }, []);

      useEffect(function () {
        run(true);
        return function () {
          epoch.current++;
          if (ctrl.current) { try { ctrl.current.abort(); } catch (_e) { /* ignore */ } }
        };
      }, [key]);

      useEffect(function () {
        let timer = null;
        const visible = function () {
          return typeof document === "undefined" || document.visibilityState !== "hidden";
        };
        const start = function () {
          if (timer !== null || !visible()) return;
          timer = setInterval(function () {
            if (visible() && !inflight.current) run(false);
          }, pollMs);
        };
        const stop = function () {
          if (timer !== null) { clearInterval(timer); timer = null; }
        };
        const onVis = function () {
          if (visible()) {
            start();
            if (!inflight.current && Date.now() - loadedAt.current >= pollMs) run(false);
          } else {
            stop();
          }
        };
        start();
        if (typeof document !== "undefined") document.addEventListener("visibilitychange", onVis);
        return function () {
          stop();
          if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVis);
        };
      }, [key]);

      const reload = useCallback(function () { run(false); }, [run]);
      // Never expose another key's data, even for one render frame.
      const view = st.key === key ? st : { key: key, status: "loading", data: null, error: null, loadedAt: 0, refreshing: false };
      return { state: view, reload: reload };
    }

    // --- small building blocks ---

    function Badge(props) {
      const conf = asConfidence(props.conf);
      const tip = props.tip || "";
      return h("span", {
        className: "ud-badge ud-badge-" + conf,
        "data-confidence": conf,
        tabIndex: 0,
        title: tip || undefined,
        "aria-label": CONF_LABEL[conf] + " confidence" + (tip ? ". " + tip : ""),
      }, CONF_LABEL[conf]);
    }

    function Skeleton(props) {
      const n = props.rows || 3;
      const rows = [];
      for (let i = 0; i < n; i++) rows.push(h("div", { key: i, className: "ud-skel-row", "aria-hidden": "true" }));
      return h("div", { className: cx("ud-skel", props.className), role: "status" },
        h("span", { className: "ud-sr" }, props.label || "Loading…"), rows);
    }

    function ErrorCard(props) {
      const e = props.error || { message: "Something went wrong." };
      const title = e.kind === "access" ? "Dashboard access error"
        : e.kind === "malformed" ? "Unexpected response"
        : "Could not load " + (props.what || "data");
      return h("div", { className: "ud-error", role: "alert", "data-kind": e.kind || "deck" },
        h("div", { className: "ud-error-title" }, "⚠ " + title),
        h("p", { className: "ud-error-msg" }, e.message),
        e.code ? h("p", { className: "ud-muted ud-small" }, "Code: " + e.code + (e.status ? " · HTTP " + e.status : "")) : null,
        props.onRetry && e.kind !== "access"
          ? h("button", { type: "button", className: "ud-btn", onClick: props.onRetry }, "Retry")
          : null);
    }

    function StaleTag(props) {
      return props.stale ? h("span", { className: "ud-stale", title: "Showing the last successful result; a newer request is pending or failed." }, "Stale") : null;
    }

    // --- Zone 1: subscription cards ---

    function MeterRow(props) {
      const m = props.meter;
      const conf = meterConfidence(m);
      const unit = has(UNIT_LABEL, m.unit) ? m.unit : "count";
      const ratio = progressRatio(m);
      const value = num(m.value);
      const limit = num(m.limit);
      const remaining = num(m.remaining);
      const fromHeaders = m.source === "anthropic_headers";
      // Live pull (DESIGN.md §9.1.2): same used/resets/as-of rows as headers, but read at refresh time.
      const fromOauth = m.source === "anthropic_oauth_usage";
      // wham/usage carries two disjoint shapes (DESIGN.md §9.2.1): credits (workspace spend control)
      // and percent (plan rate-limit window); the unit tells them apart.
      const fromRate = m.source === "chatgpt_wham_usage" && m.unit === "percent";
      const fromSpend = m.source === "chatgpt_wham_usage" && !fromRate;
      const reached = fromRate && remaining !== null && remaining <= 0;
      const resetsAt = str(m.resets_at);
      const tip = "Source: " + (fromRate ? "ChatGPT plan rate limit (wham/usage)" : (SOURCE_LABEL[m.source] || SOURCE_LABEL.none)) +
        ". Scope: " + (SCOPE_LABEL[m.scope] || "unknown") +
        ". Observed: " + fmtTime(m.observed_at) + "." +
        (resetsAt ? " Reset reported by the provider: " + fmtTime(resetsAt) + "." : "") +
        (fromHeaders ? " Value as of that call, not live; it is never extrapolated." : "") +
        (fromOauth ? " Read live from Anthropic when this view last refreshed; it is never extrapolated." : "") +
        (fromRate ? " Share of this plan's rate-limit window used, as of the last refresh; never extrapolated." +
          (reached ? " The provider reports the limit as reached." : "") : "") +
        (fromSpend ? " Credits reported for this seat's workspace spend limit, as of the last refresh; not USD, not messages, not a fleet total." : "");
      const parts = [];
      parts.push(h("div", { key: "head", className: "ud-meter-head" },
        h("span", { className: "ud-meter-label" }, str(m.label) || "Meter"),
        h(Badge, { conf: conf, tip: tip })));
      if (fromHeaders || fromOauth || fromRate) {
        // Utilization of a rolling window: used share + provider-reported reset; no invented remaining.
        parts.push(h("dl", { key: "vals", className: "ud-vals" },
          h("div", null, h("dt", null, "Used"), h("dd", null, value === null ? "Not reported" : fmtNumber(value, unit))),
          reached ? h("div", { "data-limit-reached": "true" }, h("dt", null, "Status"), h("dd", null, "Limit reached")) : null,
          h("div", null, h("dt", null, "Resets"), h("dd", { "data-resets-at": resetsAt || undefined }, resetsAt ? fmtTime(resetsAt) : "Not reported")),
          h("div", null, h("dt", null, "As of"), h("dd", null, fmtTime(m.observed_at)))));
      } else if (fromSpend) {
        // Workspace spend control: the provider's own used/limit/remaining credits and reset; null stays "Not reported".
        parts.push(h("dl", { key: "vals", className: "ud-vals" },
          h("div", null, h("dt", null, "Used"), h("dd", null, value === null ? "Not reported" : fmtNumber(value, unit))),
          h("div", null, h("dt", null, "Limit"), h("dd", null, limit === null ? "Not reported" : fmtNumber(limit, unit))),
          h("div", null, h("dt", null, "Remaining"), h("dd", null, remaining === null ? "Not reported" : fmtNumber(remaining, unit))),
          h("div", null, h("dt", null, "Resets"), h("dd", { "data-resets-at": resetsAt || undefined }, resetsAt ? fmtTime(resetsAt) : "Not reported")),
          h("div", null, h("dt", null, "As of"), h("dd", null, fmtTime(m.observed_at)))));
      } else {
        parts.push(h("dl", { key: "vals", className: "ud-vals" },
          h("div", null, h("dt", null, "Used"), h("dd", null, value === null ? "Not reported" : fmtNumber(value, unit))),
          h("div", null, h("dt", null, "Limit"), h("dd", null, limit === null ? "Not reported" : fmtNumber(limit, unit))),
          h("div", null, h("dt", null, remaining !== null && remaining < 0 ? "Overspent" : "Remaining"),
            h("dd", null, remaining === null ? "Not reported" : fmtNumber(remaining < 0 ? -remaining : remaining, unit)))));
      }
      if (ratio !== null) {
        const pct = Math.round(ratio * 1000) / 10;
        parts.push(h("div", {
          key: "bar", className: cx("ud-bar", ratio > 1 && "ud-bar-over"), role: "progressbar",
          "aria-label": (str(m.label) || "Meter") + " usage", "aria-valuemin": 0, "aria-valuemax": 100,
          "aria-valuenow": Math.min(100, pct), "aria-valuetext": pct + "% of limit" + (ratio > 1 ? " (over limit)" : ""),
        }, h("div", { className: "ud-bar-fill", style: { width: Math.min(100, ratio * 100) + "%" } })));
        parts.push(h("div", { key: "pct", className: "ud-small" },
          ratio > 1 ? "Over limit: " + pct + "% of limit" : pct + "% of limit"));
      } else if (limit !== null && value !== null) {
        parts.push(h("div", { key: "nobar", className: "ud-muted ud-small" },
          "No progress bar: the limit's period is not known, so usage cannot be compared to it."));
      }
      return h("li", { className: "ud-meter", "data-meter-id": str(m.id) || undefined }, parts);
    }

    /**
     * Modal "Show JSON" viewer (DESIGN.md §4 UI states): role=dialog, focus moves in on open,
     * Tab/Shift+Tab are trapped inside, Escape closes from anywhere, and the owner restores
     * focus to the opening button. Content is the already-sanitized card DTO as text only.
     */
    function JsonDialog(props) {
      const dialogRef = useRef(null);
      const closeRef = useRef(null);
      const onCloseRef = useRef(props.onClose);
      onCloseRef.current = props.onClose;

      useEffect(function () {
        if (closeRef.current && closeRef.current.focus) closeRef.current.focus();
        const doc = (dialogRef.current && dialogRef.current.ownerDocument) || document;
        function onDocKey(e) {
          if (e.key === "Escape" || e.key === "Esc") { e.preventDefault(); onCloseRef.current(); }
        }
        doc.addEventListener("keydown", onDocKey);
        return function () { doc.removeEventListener("keydown", onDocKey); };
      }, []);

      function trapTab(e) {
        if (e.key !== "Tab" || !dialogRef.current) return;
        const nodes = Array.prototype.slice.call(dialogRef.current.querySelectorAll("button, [tabindex='0']"));
        if (nodes.length === 0) { e.preventDefault(); return; }
        const first = nodes[0], last = nodes[nodes.length - 1];
        const active = dialogRef.current.ownerDocument.activeElement;
        const inside = nodes.indexOf(active) !== -1;
        if (e.shiftKey && (active === first || !inside)) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && (active === last || !inside)) { e.preventDefault(); first.focus(); }
      }

      return h("div", {
        id: props.id, className: "ud-drawer", role: "dialog", "aria-modal": "true",
        "aria-label": props.label, tabIndex: -1, ref: dialogRef, onKeyDown: trapTab,
      },
        h("div", { className: "ud-drawer-head" },
          h("strong", { className: "ud-small" }, props.label),
          h("button", { type: "button", className: "ud-btn ud-btn-ghost", ref: closeRef, onClick: props.onClose }, "Close")),
        h("pre", { className: "ud-drawer-json", tabIndex: 0 }, props.text));
    }

    function SubscriptionCard(props) {
      const card = props.card;
      const open = props.open === true;
      const toggleRef = useRef(null);
      const drawerId = "ud-drawer-" + sanitizeDomId(card.id || props.index);
      const view = cardView(card);
      const conf = cardConfidence(card);
      const stale = card.stale === true || props.zoneStale;
      const meters = (Array.isArray(card.meters) ? card.meters : []).filter(isMeasured);
      const plan = str(card.plan_name);
      const planText = plan
        ? "Plan: " + plan + (card.plan_source === "unverified_claim" ? " (unverified claim)" : "")
        : "Plan unknown";
      const reasonText = has(REASON_TEXT, card.reason) ? REASON_TEXT[card.reason] : null;
      const note = opaqueNote(card);
      const machineNote = machineOnlyNote(card);
      const holders = holderNames(card);

      function closeDrawer() {
        props.onOpenChange(false);
        if (toggleRef.current && toggleRef.current.focus) toggleRef.current.focus();
      }

      let body;
      if (view === "error") {
        const e = isObj(card.error) ? deckErrorFrom({ error: card.error }) : null;
        body = h(ErrorCard, {
          what: "this account",
          error: e ? Object.assign({ kind: "deck", status: null }, e)
            : { kind: "deck", status: null, code: null, message: "This account's data could not be read." },
          onRetry: props.onRetry,
        });
      } else if (view === "meters") {
        body = h("div", null,
          h("ul", { className: "ud-meters" }, meters.map(function (m, i) {
            return h(MeterRow, { key: str(m.id) || i, meter: m });
          })),
          card.state === "opaque" ? h("p", { className: "ud-muted ud-small" }, "Quota unavailable for this account." + (reasonText ? " " + reasonText : "")) : null);
      } else if (view === "empty") {
        body = h("p", { className: "ud-empty-text" }, "No observations for this period.");
      } else {
        body = h("div", { className: "ud-opaque", "data-opaque": "true" },
          h("p", { className: "ud-opaque-title" }, "Usage unavailable"),
          h("p", { className: "ud-small" }, "Data unavailable" + (reasonText ? ": " + reasonText : " from this source.")),
          card.reason === "box_offline" ? h("p", { className: "ud-muted ud-small", "data-field": "last-seen" }, lastSeenText(card)) : null,
          h("p", { className: "ud-muted ud-small" },
            card.kind === "self_hosted"
              ? "Endpoint health appears in the health strip below."
              : "Session analytics only — profile-wide usage appears in the chart below, not as this account's allowance."),
          note ? h("p", { className: "ud-muted ud-small", "data-opaque-note": str(card.provider_id) }, note) : null,
          machineNote ? h("p", { className: "ud-muted ud-small", "data-machine-only-note": "true" }, machineNote) : null);
      }

      return h("article", {
        className: cx("ud-card", "ud-card-" + (view === "unavailable" ? "opaque" : view), stale && "ud-card-stale"),
        "data-card-id": card.id, "data-view": view, "aria-busy": props.busy ? "true" : undefined,
        "aria-label": (str(card.provider_label) || "Provider") + " " + (str(card.account_label) || ""),
      },
        h("header", { className: "ud-card-head" },
          h("div", null,
            h("h3", { className: "ud-card-title" },
              str(card.provider_label) || str(card.provider_id) || "Provider",
              plan ? h("span", { className: "ud-card-plan", title: "Plan claim read from the credential; not verified." }, " [" + plan + "]") : null,
              holders.length ? h("span", { className: "ud-card-holders", "data-holders": holders.join(","),
                title: "Held by these Hermes homes (read from their auth.json; presence only, not validated)." },
                " — " + holders.join(", ")) : null),
            h("div", { className: "ud-muted ud-small" }, str(card.account_label) || "Account")),
          h("div", { className: "ud-card-badges" },
            card.kind === "self_hosted" ? h("span", { className: "ud-kind" }, "Self-hosted endpoint") : null,
            h(Badge, { conf: conf, tip: "Primary source for this card. Checked: " + fmtTime(card.checked_at) + "." + (note && conf === "opaque" ? " " + note : "") + (machineNote ? " " + machineNote : "") }))),
        h("div", { className: "ud-card-meta ud-small" },
          h("span", null, planText), " · ",
          h("span", { "data-auth-status": authInfo(card.auth_status).key, title: authInfo(card.auth_status).tip },
            authInfo(card.auth_status).text),
          stale ? h("span", null, " · ", h(StaleTag, { stale: true })) : null),
        h("div", { className: "ud-card-body" }, body),
        h("footer", { className: "ud-card-foot" },
          h("span", { className: "ud-muted ud-small" }, "Checked " + fmtTime(card.checked_at)),
          h("button", {
            type: "button", className: "ud-btn ud-btn-ghost", ref: toggleRef,
            "aria-expanded": open ? "true" : "false", "aria-controls": drawerId,
            onClick: function () { if (open) closeDrawer(); else props.onOpenChange(true); },
          }, open ? "Hide JSON" : "Show JSON")),
        open ? h(JsonDialog, {
          id: drawerId, label: "Sanitized JSON for " + (str(card.provider_label) || "card"),
          text: JSON.stringify(card, null, 2), onClose: closeDrawer,
        }) : null);
    }

    function SubscriptionsZone(props) {
      const st = props.res.state;
      const reload = props.res.reload;
      const machine = props.view === "machine";
      const [openId, setOpenId] = useState(null);
      // A dialog never outlives the filter change that may hide its card.
      useEffect(function () { setOpenId(null); }, [props.provider]);
      let content;
      if (st.status === "loading" && !st.data) {
        content = h("div", { className: "ud-grid", "aria-busy": "true" },
          [0, 1, 2].map(function (i) { return h("div", { key: i, className: "ud-card ud-card-loading" }, h(Skeleton, { rows: 4, label: "Loading subscriptions…" })); }));
      } else if (st.status === "error" && !st.data) {
        content = h(ErrorCard, { what: "subscriptions", error: st.error, onRetry: reload });
      } else {
        const data = st.data;
        const stale = st.status === "error" || st.refreshing;
        const err = st.status === "error" ? h(ErrorCard, { key: "err", what: "subscriptions", error: st.error, onRetry: reload }) : null;
        if (data.cards.length === 0) {
          content = h("div", null, err,
            h("div", { className: "ud-first-run", "data-state": "first_run" },
              h("p", { className: "ud-first-run-title" }, machine ? "No providers detected on this machine" : "No providers detected in this profile"),
              h("p", { className: "ud-small" }, (machine
                ? "UsageDeck reads the existing credential pools of the global Hermes home and every profile; it never asks for keys."
                : "UsageDeck reads this profile's existing Hermes configuration and credential pool; it never asks for keys.") + " Add or configure a provider in the dashboard's Keys or Config pages and it will appear here."),
              h("button", { type: "button", className: "ud-btn", onClick: reload }, "Refresh")));
        } else {
          // The page-level provider filter narrows this list too (not only the usage chart).
          const shown = props.provider
            ? data.cards.filter(function (c) {
              const id = str(c.provider_id);
              return id === props.provider || (props.provider === "custom" && id.indexOf("custom:") === 0);
            })
            : data.cards;
          content = h("div", null, err,
            props.provider
              ? h("p", { className: "ud-muted ud-small", "data-filter-note": "true" },
                "Showing " + shown.length + " of " + data.cards.length + " subscription cards (provider filter).")
              : null,
            h("div", { className: "ud-grid", "aria-busy": st.refreshing ? "true" : undefined },
              shown.map(function (c, i) {
                const key = c.id || i;
                return h(SubscriptionCard, {
                  key: key, index: i, card: c, zoneStale: stale, busy: st.refreshing, onRetry: reload,
                  open: openId === key,
                  onOpenChange: function (o) { setOpenId(o ? key : null); },
                });
              })));
        }
      }
      return h("section", { className: "ud-zone", "aria-labelledby": "ud-h-subs", "data-zone": "subscriptions" },
        h("div", { className: "ud-zone-head" },
          h("h2", { id: "ud-h-subs" }, "Subscriptions"),
          st.data && (st.status === "error" || st.refreshing) ? h(StaleTag, { stale: true }) : null,
          h("div", { className: "ud-controls" },
            h("div", { role: "group", "aria-label": "Subscription scope", className: "ud-seg", "data-view-toggle": props.view },
              VIEWS.map(function (v) {
                return h("button", {
                  key: v, type: "button", className: cx("ud-btn", "ud-seg-btn", props.view === v && "ud-seg-on"),
                  "aria-pressed": props.view === v ? "true" : "false", "data-view": v,
                  onClick: function () { props.onView(v); },
                }, v === "machine" ? "Whole machine" : "This profile");
              })))),
        h("p", { className: "ud-muted ud-small", "data-view-note": props.view },
          machine
            ? "Machine view: the global Hermes home plus every profile's credential pool (auth.json only), merged only when a Codex login carries the same account and user identity. Each card lists the profiles that hold it; accounts held only by other profiles are inventory and are never probed with their credentials."
            : "Profile view: the dashboard's selected profile, with global fallback per provider. Cards are not merged across profiles."),
        content);
    }

    // --- Zone 2: usage chart ---

    function LineChart(props) {
      // props: title, unit, lines:[{name, field, cls}], points
      const W = 640, H = 190, L = 64, R = 14, T = 12, B = 30;
      const pts = sortedByDay(props.points);
      const all = [];
      props.lines.forEach(function (ln) {
        splitSegments(pts, ln.field).forEach(function (seg) { seg.forEach(function (p) { all.push(p.v); }); });
      });
      if (all.length === 0) {
        return h("div", { className: "ud-chart-empty", "data-chart-empty": props.title },
          h("strong", null, props.title), h("p", { className: "ud-muted ud-small" }, "No observations for this period."));
      }
      const ts = pts.map(function (p) { return dayToMs(p.day); });
      const tmin = Math.min.apply(null, ts), tmax = Math.max.apply(null, ts);
      const ymax = niceMax(Math.max.apply(null, all));
      const x = function (t) { return tmax === tmin ? L + (W - L - R) / 2 : L + ((t - tmin) / (tmax - tmin)) * (W - L - R); };
      const y = function (v) { return T + (1 - v / ymax) * (H - T - B); };
      const els = [];
      [0, 0.5, 1].forEach(function (f, i) {
        const v = ymax * f;
        els.push(h("g", { key: "tick" + i },
          h("line", { className: "ud-grid-line", x1: L, x2: W - R, y1: y(v), y2: y(v) }),
          h("text", { className: "ud-axis", x: L - 6, y: y(v) + 4, textAnchor: "end" }, fmtNumber(v, props.unit))));
      });
      els.push(h("text", { key: "x0", className: "ud-axis", x: L, y: H - 8, textAnchor: "start" }, pts[0].day));
      if (pts.length > 1) els.push(h("text", { key: "x1", className: "ud-axis", x: W - R, y: H - 8, textAnchor: "end" }, pts[pts.length - 1].day));
      props.lines.forEach(function (ln) {
        splitSegments(pts, ln.field).forEach(function (seg, si) {
          if (seg.length > 1) {
            els.push(h("polyline", {
              key: ln.field + "-s" + si, className: "ud-line " + ln.cls, fill: "none",
              points: seg.map(function (p) { return x(p.t).toFixed(1) + "," + y(p.v).toFixed(1); }).join(" "),
            }));
          }
          seg.forEach(function (p) {
            els.push(h("circle", { key: ln.field + "-" + p.day, className: "ud-dot " + ln.cls, cx: x(p.t), cy: y(p.v), r: 3 },
              h("title", null, p.day + " — " + ln.name + ": " + fmtNumber(p.v, props.unit))));
          });
        });
      });
      return h("figure", { className: "ud-chart", "data-chart": props.title },
        h("figcaption", { className: "ud-chart-title" }, props.title,
          h("span", { className: "ud-legend" }, props.lines.map(function (ln) {
            return h("span", { key: ln.field, className: "ud-legend-item" },
              h("svg", { width: 22, height: 8, "aria-hidden": "true" }, h("line", { className: "ud-line " + ln.cls, x1: 0, x2: 22, y1: 4, y2: 4 })),
              ln.name);
          }))),
        h("svg", { viewBox: "0 0 " + W + " " + H, className: "ud-svg", role: "img", "aria-label": props.title + ", " + pts.length + " days with data" },
          els));
    }

    /** A metric cell: dash for unavailable, 0 for a measured zero, `*` on a known subtotal that misses sessions. */
    function MetricCell(props) {
      const m = props.metrics;
      const v = num(m && m[props.field]);
      const partial = v !== null && Array.isArray(m.missing_fields) && m.missing_fields.indexOf(props.field) >= 0;
      return h("td", { "data-field": props.field, "data-partial": partial ? "true" : undefined,
        title: partial ? "Known subtotal: some sessions did not record this value." : (v === null ? "Not recorded." : undefined) },
        fmtMetric(v, props.unit), partial ? h("span", { "aria-hidden": "true" }, "*") : null);
    }

    function TotalTokensCell(props) {
      const t = totalTokens(props.metrics);
      return h("td", { "data-field": "total_tokens", "data-partial": t.partial ? "true" : undefined,
        title: t.partial ? "Known subtotal: only one of input / output was recorded." : undefined },
        t.value === null ? "—" : fmtNumber(t.value, "count"), t.partial ? h("span", { "aria-hidden": "true" }, "*") : null);
    }

    /** Visible API-equivalent qualifiers (partial / unpriced / price proxy / stale prices). */
    function ApiFlags(props) {
      return apiFlags(props.view).map(function (f) {
        return h("span", { key: f.id, className: "ud-api-flag", "data-api-flag": f.id }, f.text);
      });
    }

    /** API-equivalent ($) cell: dash for null, $0.00 for an exact zero (data-api-state tells them apart). */
    function ApiCell(props) {
      const v = apiView(props.metrics);
      return h("td", { className: "ud-api-cell", "data-field": "api_equivalent_usd", "data-api-state": v.state,
        "data-api-pico": v.pico === null ? undefined : v.pico, "data-partial": v.partial ? "true" : undefined,
        "data-row-count": typeof props.rowCount === "number" ? String(props.rowCount) : undefined,
        title: apiTitle(v, props.row, props.rowCount) },
      h("span", { className: "ud-api-amount" }, v.text), h(ApiFlags, { view: v }));
    }

    /** The same value inside the totals card. */
    function ApiTotal(props) {
      const v = apiView(props.metrics);
      return h("span", { className: "ud-api-cell", "data-field": "api_equivalent_usd", "data-api-state": v.state,
        "data-api-pico": v.pico === null ? undefined : v.pico, "data-partial": v.partial ? "true" : undefined,
        title: apiTitle(v, null, props.rowCount) },
      h("span", { className: "ud-api-amount" }, v.text), h(ApiFlags, { view: v }));
    }

    /** Explicit unpriced bucket: exact model ids that no price could be found for, with the reason; never summed. */
    function UnpricedModels(props) {
      const groups = unpricedModels(props.data);
      if (groups.length === 0) return null;
      const total = groups.reduce(function (n, g) { return n + g.rows; }, 0);
      return h("details", { className: "ud-table-wrap", "data-unpriced-models": "true" },
        h("summary", null, "Unpriced models (" + total + " daily row" + (total === 1 ? "" : "s") + ")"),
        h("p", { className: "ud-muted ud-small" }, "These rows have no API-equivalent value. They are shown as \u2014 and are excluded from every sum; they are not zero."),
        h("ul", { className: "ud-small" }, groups.map(function (g) {
          return h("li", { key: g.model + "|" + (g.code || ""), "data-unpriced-model": g.model, "data-reason": g.code || undefined },
            g.model + " \u2014 " + g.reason + " (" + g.rows + " daily row" + (g.rows === 1 ? "" : "s") + ")");
        })));
    }

    function PricingNotes(props) {
      return pricingNotes(props.data).map(function (n) {
        return h("p", { key: n.code, className: "ud-muted ud-small", "data-pricing-warning": n.code }, n.text);
      });
    }

    function apiHead(c) {
      return h("th", { key: c, scope: "col", title: c === API_LABEL ? API_TIP : undefined }, c);
    }

    /** Subordinate line of the totals area: completed-session duration percentiles (null stays a dash, never 0). */
    function DurationLine(props) {
      const dp = props.depth;
      if (!dp || !dp.supported) return null;  // an older server: nothing to show, nothing invented
      if (dp.durations === null) {
        return h("p", { className: "ud-muted ud-small", "data-duration-summary": "unavailable" }, DURATION_LABEL + ": unavailable (session stores could not be read).");
      }
      const items = durationItems(dp.durations, dp.samples);
      const out = [DURATION_LABEL + ": "];
      items.forEach(function (it, i) {
        if (i > 0) out.push(" \u00b7 ");
        out.push(h("span", { key: it.id, title: it.title, "data-duration": it.id }, it.label + " " + it.text));
      });
      return h("p", { className: "ud-muted ud-small", "data-duration-summary": "true" }, out);
    }

    /** Whole-scope totals: independent of the dimension, the top-N cut and the legend. */
    function TotalsBar(props) {
      const t = props.totals;
      const partial = Array.isArray(t.missing_fields) && t.missing_fields.length > 0;
      const tt = totalTokens(t);
      const items = [
        ["Input", fmtMetric(t.input_count, "count")],
        ["Output", fmtMetric(t.output_count, "count")],
        ["Total tokens", tt.value === null ? "—" : fmtNumber(tt.value, "count")],
        ["Est. USD", fmtMetric(t.estimated_usd, "usd")],
        [API_LABEL, h(ApiTotal, { metrics: t, rowCount: props.rowCount }), API_TIP],
        ["Sessions", fmtMetric(t.sessions, "count")],
        ["API calls", fmtMetric(t.api_calls, "count")],
      ];
      return h("div", { className: "ud-totals", "data-usage-totals": "true" },
        h("p", { className: "ud-muted ud-small" }, "Totals for the whole filtered scope, including any group hidden in the legend."),
        h("dl", { className: "ud-vals" }, items.map(function (it) {
          return h("div", { key: it[0] }, h("dt", { title: it[2] }, it[0]), h("dd", null, it[1]));
        })),
        h(DurationLine, { depth: props.depth }),
        h("p", { className: "ud-muted ud-small", "data-api-footnote": "true" }, API_FOOTNOTE),
        partial ? h("p", { className: "ud-muted ud-small", "data-totals-partial": "true" },
          "* Some sessions did not record: " + t.missing_fields.join(", ") + ". Those figures are known subtotals; nothing is estimated for the missing values.") : null);
    }

    /** Activity by start time: the exact start epochs are re-bucketed by the BROWSER's zone rules (never a rotated UTC grid). */
    function HeatmapCard(props) {
      const dp = props.depth;
      const [metric, setMetric] = useState("sessions");
      const [sel, setSel] = useState(null);
      const starts = dp && dp.heatmap ? dp.heatmap.starts : null;
      const cells = useMemo(function () { return starts ? localHeat(starts) : null; }, [starts]);
      if (!dp || !dp.supported) return null;
      const partial = !!(dp.status && dp.status.partial);
      let body;
      if (dp.heatmap === null) {
        body = h("p", { className: "ud-empty-text", "data-depth-state": "unavailable" }, "Activity heatmap unavailable: the session stores could not be read.");
      } else if (cells === null) {
        body = h("p", { className: "ud-empty-text", "data-depth-state": "no-local" }, "Local heatmap unavailable");
      } else {
        const max = heatMax(cells, metric);
        const picked = sel && cells[sel.row * 24 + sel.col];
        const detail = sel ? heatDetail(starts, sel.row, sel.col) : [];
        body = h("div", null,
          h("div", { className: "ud-table-scroll" },
            h("table", { className: "ud-heat", "data-heat-grid": "local", "data-heat-metric": metric },
              h("caption", { className: "ud-sr-only" }, "Sessions started per local weekday and hour"),
              h("thead", null, h("tr", null, h("th", { scope: "col" }, "Day"), Array.from({ length: 24 }, function (_, c) {
                return h("th", { key: c, scope: "col" }, depthPad(c));
              }))),
              h("tbody", null, DEPTH_DAYS.map(function (day, r) {
                return h("tr", { key: day },
                  h("th", { scope: "row" }, day),
                  Array.from({ length: 24 }, function (_, c) {
                    const cell = cells[r * 24 + c];
                    const lv = heatLevel(cell, metric, max);
                    const label = heatCellLabel(r, c, cell, partial);
                    const on = !!sel && sel.row === r && sel.col === c;
                    return h("td", { key: c }, h("button", {
                      type: "button", className: cx("ud-heat-cell", "ud-heat-l" + lv.level, lv.na && "ud-heat-na"),
                      "aria-label": label, title: label, "aria-pressed": on ? "true" : "false",
                      "data-heat-cell": r + "-" + c, "data-sessions": String(cell.sessions),
                      "data-tokens": cell.tokens === null ? "null" : String(cell.tokens), "data-heat-na": lv.na ? "true" : undefined,
                      onClick: function () { setSel(on ? null : { row: r, col: c }); },
                    }, lv.na ? "?" : (cell.sessions > 0 ? String(cell.sessions) : "")));
                  }));
              })))),
          sel && picked ? h("div", { className: "ud-heat-detail", "data-heat-detail": sel.row + "-" + sel.col, role: "region",
            "aria-label": "Sessions started " + DEPTH_DAYS[sel.row] + " " + depthPad(sel.col) + ":00" },
          h("p", { className: "ud-small" }, heatCellLabel(sel.row, sel.col, picked, partial)),
          detail.length === 0 ? h("p", { className: "ud-muted ud-small" }, "No sessions started in this hour.") :
            h("table", { className: "ud-table" },
              h("thead", null, h("tr", null, ["Start (instant)", "Local offset", "Sessions", "Tokens"].map(function (c) { return h("th", { key: c, scope: "col" }, c); }))),
              h("tbody", null, detail.map(function (e) {
                return h("tr", { key: e.t, "data-heat-start": String(e.t) },
                  h("td", null, e.iso), h("td", null, e.offset), h("td", null, String(e.sessions)),
                  h("td", null, e.tokens === null ? "\u2014" : depthGroup(e.tokens)));
              })))) : null);
      }
      return h("section", { className: "ud-card ud-depth-card", "data-depth-card": "heatmap", "aria-labelledby": "ud-h-heat" },
        h("h3", { id: "ud-h-heat" }, "Activity by start time (local)"),
        h("p", { className: "ud-muted ud-small", "data-heat-caption": "true" }, "Time zone: " + localZoneName() + ". " + HEAT_NOTE),
        h("div", { role: "group", "aria-label": "Heatmap intensity", className: "ud-seg" }, ["sessions", "tokens"].map(function (m) {
          return h("button", { key: m, type: "button", className: cx("ud-btn", "ud-seg-btn", metric === m && "ud-seg-on"),
            "aria-pressed": metric === m ? "true" : "false", "data-heat-toggle": m, onClick: function () { setMetric(m); } },
          m === "sessions" ? "Sessions" : "Tokens");
        })),
        body,
        depthStatusLine(dp.status) ? h("p", { className: "ud-muted ud-small", "data-depth-status": "true" }, depthStatusLine(dp.status)) : null);
    }

    /** Cache economics per exact model id (recorded-cost basis); provider is a displayed attribute (resolved wins over Unknown): no top-N, no price fetch, no JS money. */
    function CacheCard(props) {
      const dp = props.depth;
      if (!dp || !dp.supported) return null;
      const cols = ["Model", "Provider", "Hit %", "Cached input", "Uncached input", "Cache writes", "Reasoning", "Reasoning share", CACHE_LABEL, "Details"];
      let body;
      if (dp.cache === null) {
        body = h("p", { className: "ud-empty-text", "data-depth-state": "unavailable" }, "Cache economics unavailable: the session stores could not be read.");
      } else if (dp.cache.length === 0) {
        body = h("p", { className: "ud-empty-text", "data-depth-state": "empty" }, "No sessions in this scope.");
      } else {
        body = h("div", { className: "ud-table-scroll ud-cache-scroll" },
          h("table", { className: "ud-table", "data-cache-table": "true" },
            h("thead", null, h("tr", null, cols.map(function (c) {
              return h("th", { key: c, scope: "col", title: c === CACHE_LABEL ? CACHE_TIP : undefined }, c);
            }))),
            h("tbody", null, dp.cache.map(function (r, i) {
              const v = cacheRowView(r);
              return h("tr", { key: i, "data-cache-row": v.model + "|" + (v.provider || ""), "data-cache-reason": v.reason || undefined },
                h("td", null, v.model),
                h("td", { "data-field": "provider_id" }, v.provider === null ? "Unknown" : providerList(v.provider).map(function (id) { return providerLabelOf(props.providers, id); }).join(", ")),
                h("td", { "data-field": "hit_rate_pct" }, v.hit),
                h("td", { "data-field": "cached_input_tokens" }, v.cached),
                h("td", { "data-field": "uncached_input_tokens" }, v.uncached),
                h("td", { "data-field": "cache_write_tokens" }, v.writes),
                h("td", { "data-field": "reasoning_tokens" }, v.reasoning),
                h("td", { "data-field": "reasoning_share_pct" }, v.share),
                h("td", { "data-field": "effective_cost_per_mtok", title: v.effectiveTitle }, v.effective),
                h("td", null, h("details", { className: "ud-cache-detail" }, h("summary", null, "Details"),
                  h("dl", { className: "ud-small" },
                    h("dt", null, "Output tokens"), h("dd", { "data-field": "output_tokens" }, v.output),
                    h("dt", null, "Cache discount multiplier"), h("dd", { "data-field": "discount_multiplier" }, v.multiplier),
                    h("dt", null, "Rate snapshot"), h("dd", { "data-field": "rate_source" }, v.rateSource),
                    v.reasonText ? h("dt", null, "Why a value is missing") : null, v.reasonText ? h("dd", { "data-field": "reason" }, v.reasonText) : null))));
            }))));
      }
      return h("section", { className: "ud-card ud-depth-card", "data-depth-card": "cache", "aria-labelledby": "ud-h-cache" },
        h("h3", { id: "ud-h-cache" }, "Cache economics \u00b7 recorded-cost basis"),
        h("p", { className: "ud-muted ud-small" }, CACHE_NOTE + " " + CACHE_TIP),
        body,
        depthStatusLine(dp.status) ? h("p", { className: "ud-muted ud-small", "data-depth-status": "true" }, depthStatusLine(dp.status)) : null);
    }

    function CoverageNote(props) {
      const d = props.data;
      if (d.complete) return null;
      const bad = d.sources.filter(function (s) { return s.state === "unavailable" || s.state === "excluded"; });
      return h("div", { className: "ud-error", role: "status", "data-coverage": "incomplete" },
        h("div", { className: "ud-error-title" }, "⚠ " + (d.mode === "machine" ? "Incomplete machine coverage" : "Incomplete coverage")),
        h("p", { className: "ud-error-msg" }, "Totals below cover only the session stores that could be read; nothing is guessed for the others."),
        h("ul", { className: "ud-small", "data-sources": "true" }, d.sources.map(function (s, i) {
          const name = s.is_global === true ? "global (default home)" : str(s.profile) || "profile";
          return h("li", { key: i, "data-source-state": str(s.state) || undefined }, name + " — " + (str(s.state) || "unknown"));
        })),
        bad.length === 0 ? h("p", { className: "ud-muted ud-small" }, "Some profile stores were not enumerated (safety bound or unreadable directory).") : null);
    }

    /** Daily provenance rows: which profile DB, model and provider each contribution came from. */
    function RowsTable(props) {
      const rows = props.rows.slice().sort(function (a, b) {
        return (a.day < b.day ? -1 : a.day > b.day ? 1 : 0) || (str(a.profile) < str(b.profile) ? -1 : 1);
      });
      const shown = rows.slice(0, 500);
      const cols = (props.withGroup ? ["Group"] : []).concat(["Day", "Profile DB", "Model", "Provider", "Input", "Output", "Est. USD", API_LABEL, "Sessions", "API calls"]);
      return h("div", { className: "ud-table-scroll" },
        h("table", { className: "ud-table", "data-rows-table": props.name || "rows" },
          h("thead", null, h("tr", null, cols.map(apiHead))),
          h("tbody", null, shown.map(function (r) {
            const attr = str(r.attribution_source);
            const note = attr === "billing_provider" ? " (billing)" : attr === "models_json" ? " (catalog)" : "";
            return h("tr", { key: r.id, "data-row-id": r.id },
              props.withGroup ? h("td", null, props.groupLabel(r)) : null,
              h("td", null, r.day),
              h("td", null, profileDbLabel(r)),
              h("td", null, str(r.model_id) || "Unknown model"),
              h("td", { title: has(ATTRIBUTION_TIP, attr) ? ATTRIBUTION_TIP[attr] : undefined, "data-attribution": attr || undefined },
                providerLabelOf(props.providers, r.provider_id) + note),
              h(MetricCell, { metrics: r.metrics, field: "input_count" }),
              h(MetricCell, { metrics: r.metrics, field: "output_count" }),
              h(MetricCell, { metrics: r.metrics, field: "estimated_usd", unit: "usd" }),
              h(ApiCell, { metrics: r.metrics, row: r }),
              h(MetricCell, { metrics: r.metrics, field: "sessions" }),
              h(MetricCell, { metrics: r.metrics, field: "api_calls" }));
          }))),
        rows.length > shown.length ? h("p", { className: "ud-muted ud-small" }, "Showing the first " + shown.length + " of " + rows.length + " daily rows.") : null);
    }

    function SeriesBlock(props) {
      const s = props.series;
      const conf = effectiveConfidence(s.confidence, SOURCE_CEILING[s.source] || "opaque");
      const pts = Array.isArray(s.points) ? s.points : [];
      const scopeText = s.scope === "machine" ? "all local profiles" : s.scope === "profile" ? "this Hermes profile" : "provider-attributed";
      const tip = "Source: Hermes session analytics. Scope: " + scopeText +
        ". Period: " + (isObj(s.period) && s.period.start ? fmtTime(s.period.start) + " to " + fmtTime(s.period.end) : "rolling window") +
        ". Checked: " + fmtTime(s.checked_at) + ".";
      if (isObj(s.error)) {
        const e = deckErrorFrom({ error: s.error });
        return h(ErrorCard, { what: "this series", error: e ? Object.assign({ kind: "deck", status: null }, e) : { kind: "deck", message: "This series could not be read." } });
      }
      const sorted = sortedByDay(pts);
      return h("div", { className: "ud-series", "data-series-id": str(s.id) || undefined },
        h("div", { className: "ud-series-head" },
          h(Badge, { conf: conf, tip: tip }),
          h("span", { className: "ud-muted ud-small" },
            "Days in server local time · " + (s.coverage === "complete" ? "complete coverage" : "partial coverage (first day partial, telemetry may be incomplete)") + " · checked " + fmtTime(s.checked_at)),
          s.stale === true ? h(StaleTag, { stale: true }) : null),
        sorted.length === 0
          ? h("p", { className: "ud-empty-text" }, "No observations for this period.")
          : h("div", { className: "ud-charts" },
            h(LineChart, { title: "Tokens (count)", unit: "count", points: sorted, lines: [
              { name: "Input", field: "input_count", cls: "ud-line-a" },
              { name: "Output", field: "output_count", cls: "ud-line-b" },
            ] }),
            h(LineChart, { title: "Estimated spend (USD)", unit: "usd", points: sorted, lines: [
              { name: "Estimated USD", field: "estimated_usd", cls: "ud-line-a" },
            ] })),
        sorted.length === 0 ? null : h("details", { className: "ud-table-wrap" },
          h("summary", null, "Data table"),
          h("table", { className: "ud-table" },
            h("thead", null, h("tr", null, ["Day", "Input", "Output", "Est. USD", API_LABEL, "Sessions", "API calls"].map(apiHead))),
            h("tbody", null, sorted.map(function (p) {
              return h("tr", { key: p.day },
                h("td", null, p.day),
                h("td", null, fmtNumber(p.input_count, "count")),
                h("td", null, fmtNumber(p.output_count, "count")),
                h("td", null, fmtNumber(p.estimated_usd, "usd")),
                h(ApiCell, { metrics: p }),
                h("td", null, fmtNumber(p.sessions, "count")),
                h("td", null, fmtNumber(p.api_calls, "count")));
            })))),
        props.rows && props.rows.length > 0 ? h("details", { className: "ud-table-wrap", "data-provenance": "true" },
          h("summary", null, "Contributions by profile DB, model and provider (" + props.rows.length + ")"),
          h(RowsTable, { rows: props.rows, providers: props.providers, name: "provenance" })) : null);
    }

    /** One chart over several groups: every line has its own points; null breaks a line, never a zero. */
    function GroupChart(props) {
      const W = 640, H = 190, L = 64, R = 14, T = 12, B = 30;
      const all = [];
      const days = [];
      props.lines.forEach(function (ln) {
        splitSegments(ln.points, ln.field).forEach(function (seg) { seg.forEach(function (p) { all.push(p.v); days.push(p.t); }); });
      });
      if (all.length === 0) {
        return h("div", { className: "ud-chart-empty", "data-chart-empty": props.title },
          h("strong", null, props.title), h("p", { className: "ud-muted ud-small" }, "No observations for this period."));
      }
      const tmin = Math.min.apply(null, days), tmax = Math.max.apply(null, days);
      const ymax = niceMax(Math.max.apply(null, all));
      const x = function (t) { return tmax === tmin ? L + (W - L - R) / 2 : L + ((t - tmin) / (tmax - tmin)) * (W - L - R); };
      const y = function (v) { return T + (1 - v / ymax) * (H - T - B); };
      const els = [];
      [0, 0.5, 1].forEach(function (f, i) {
        const v = ymax * f;
        els.push(h("g", { key: "tick" + i },
          h("line", { className: "ud-grid-line", x1: L, x2: W - R, y1: y(v), y2: y(v) }),
          h("text", { className: "ud-axis", x: L - 6, y: y(v) + 4, textAnchor: "end" }, fmtNumber(v, props.unit))));
      });
      const first = new Date(tmin).toISOString().slice(0, 10), last = new Date(tmax).toISOString().slice(0, 10);
      els.push(h("text", { key: "x0", className: "ud-axis", x: L, y: H - 8, textAnchor: "start" }, first));
      if (tmax !== tmin) els.push(h("text", { key: "x1", className: "ud-axis", x: W - R, y: H - 8, textAnchor: "end" }, last));
      props.lines.forEach(function (ln) {
        splitSegments(ln.points, ln.field).forEach(function (seg, si) {
          if (seg.length > 1) {
            els.push(h("polyline", {
              key: ln.key + "-s" + si, className: "ud-line ud-g" + ln.color + (ln.dash ? " ud-dash" : ""), fill: "none",
              "data-group-line": ln.groupId, "data-metric": ln.field,
              points: seg.map(function (p) { return x(p.t).toFixed(1) + "," + y(p.v).toFixed(1); }).join(" "),
            }));
          }
          seg.forEach(function (p) {
            els.push(h("circle", { key: ln.key + "-" + p.day, className: "ud-dot ud-g" + ln.color, cx: x(p.t), cy: y(p.v), r: 3,
              "data-group-line": ln.groupId, "data-metric": ln.field },
            h("title", null, ln.tip(p))));
          });
        });
      });
      return h("figure", { className: "ud-chart", "data-chart": props.title },
        h("figcaption", { className: "ud-chart-title" }, props.title,
          props.note ? h("span", { className: "ud-legend" }, props.note) : null),
        h("svg", { viewBox: "0 0 " + W + " " + H, className: "ud-svg", role: "img", "aria-label": props.title + ", " + props.lines.length + " lines" }, els));
    }

    function pointTip(view, p) {
      const g = view.group;
      const owners = (Array.isArray(g.profiles) ? g.profiles : []).map(function (o) { return o.is_global === true ? "global" : str(o.profile); }).filter(Boolean);
      return str(g.label) + " — " + p.day + ": input " + fmtMetric(p.input_count) + ", output " + fmtMetric(p.output_count) +
        ", est. " + fmtMetric(p.estimated_usd, "usd") + (owners.length ? "; profiles: " + owners.join(", ") : "");
    }

    /** By profile / By model / By repo: one shared legend drives both charts and the grouped tables (no refetch). */
    function GroupDurations(props) {
      const dp = props.depth;
      if (!dp || !dp.supported) return null;
      const withStats = props.views.filter(function (v) { return isObj(v.group.durations); });
      if (withStats.length === 0) return null;
      return h("details", { className: "ud-table-wrap", "data-group-durations": "true" },
        h("summary", null, DURATION_LABEL + " by group"),
        h("div", { className: "ud-table-scroll" },
          h("table", { className: "ud-table" },
            h("thead", null, h("tr", null, ["Group", "p50", "p90", "p99", "mean", "max", "samples"].map(function (c) { return h("th", { key: c, scope: "col" }, c); }))),
            h("tbody", null, withStats.map(function (v) {
              const items = durationItems(v.group.durations, v.group.duration_samples);
              return h("tr", { key: v.group.id, "data-group-id": v.group.id },
                h("td", null, str(v.group.label)),
                items.map(function (it) { return h("td", { key: it.id, title: it.title, "data-duration": it.id }, it.text); }));
            })))));
    }

    /** Repository path detail: one path in full, several as "N paths" that expand to every path. Text nodes only. */
    function RepoPaths(props) {
      const dp = props.depth;
      if (!dp || !dp.repos) return null;
      const items = props.views.map(function (v) { return { v: v, p: repoPathSummary(repoRowFor(v.group, dp.repos)) }; })
        .filter(function (x) { return x.p.count > 0; });
      if (items.length === 0) return null;
      return h("details", { className: "ud-table-wrap", "data-repo-paths": "true" },
        h("summary", null, "Repository paths (" + items.length + ")"),
        h("ul", { className: "ud-small" }, items.map(function (x) {
          return h("li", { key: x.v.group.id, "data-repo-group": x.v.group.id },
            x.p.count === 1 ? str(x.v.group.label) + " \u2014 " + x.p.text
              : h("details", null, h("summary", null, str(x.v.group.label) + " \u2014 " + x.p.text),
                h("ul", null, x.p.paths.map(function (p) { return h("li", { key: p }, p); }))));
        })));
    }

    /** By profile / By model / By repo: one shared legend drives both charts and the grouped tables (no refetch). */
    function GroupedView(props) {
      const d = props.data;
      const views = useMemo(function () { return groupViews(d); }, [d]);
      const [hidden, setHidden] = useState({});
      const visible = views.filter(function (v) { return !hidden[v.group.id]; });
      const toggle = function (id) { setHidden(function (prev) { const n = Object.assign({}, prev); if (n[id]) delete n[id]; else n[id] = true; return n; }); };
      const anyHidden = views.some(function (v) { return hidden[v.group.id]; });
      const byProfile = d.dimension === "profile";
      const byRepo = d.dimension === "repo";
      const noun = byRepo ? "repository" : byProfile ? "profile" : "model";
      const depthInfo = d.depth || null;
      const repoOf = function (v) { return byRepo && depthInfo && depthInfo.repos ? repoPathSummary(repoRowFor(v.group, depthInfo.repos)) : null; };
      const coverage = byRepo && depthInfo && depthInfo.repos ? repoCoverage(depthInfo.repos) : null;
      const tokenLines = [], spendLines = [];
      visible.forEach(function (v) {
        const pts = sortedByDay(v.series.points);
        const base = { color: v.color, groupId: v.group.id, points: pts, tip: function (p) { return pointTip(v, p); } };
        tokenLines.push(Object.assign({ key: v.group.id + "-in", field: "input_count", dash: false }, base));
        tokenLines.push(Object.assign({ key: v.group.id + "-out", field: "output_count", dash: true }, base));
        spendLines.push(Object.assign({ key: v.group.id + "-usd", field: "estimated_usd", dash: false }, base));
      });
      const otherView = views.filter(function (v) { return v.group.is_other === true && !hidden[v.group.id]; })[0] || null;
      const groupOfRow = {};
      views.forEach(function (v) { v.rows.forEach(function (r) { groupOfRow[r.id] = v.group; }); });
      const mainRows = [];
      visible.forEach(function (v) { if (v.group.is_other !== true) v.rows.forEach(function (r) { mainRows.push(r); }); });
      const sumRows = visible.map(function (v) { return v; });
      return h("div", { className: "ud-grouped", "data-dimension-view": d.dimension },
        h("div", { className: "ud-group-legend", role: "group", "aria-label": byRepo ? "Repositories" : byProfile ? "Profiles" : "Models" },
          views.map(function (v) {
            const on = !hidden[v.group.id];
            return h("button", { key: v.group.id, type: "button", className: cx("ud-btn", "ud-legend-btn", on && "ud-seg-on"),
              "aria-pressed": on ? "true" : "false", "data-group-id": v.group.id, onClick: function () { toggle(v.group.id); } },
            h("svg", { width: 22, height: 8, "aria-hidden": "true" }, h("line", { className: "ud-line ud-g" + v.color, x1: 0, x2: 22, y1: 4, y2: 4 })),
            str(v.group.label));
          }),
          h("button", { type: "button", className: "ud-btn", disabled: anyHidden ? undefined : true, "data-show-all": "true",
            onClick: function () { setHidden({}); } }, "Show all")),
        d.dimension === "model" && d.top_n ? h("p", { className: "ud-muted ud-small" },
          "Top " + d.top_n + " models by known input + output tokens over this window; every remaining model is rolled up into Other.") : null,
        h("div", { className: "ud-charts" },
          h(GroupChart, { title: "Tokens (count)", unit: "count", lines: tokenLines, note: "solid = input · dashed = output" }),
          h(GroupChart, { title: "Estimated spend (USD)", unit: "usd", lines: spendLines })),
        h("div", { className: "ud-table-scroll" },
          h("table", { className: "ud-table", "data-summary-table": d.dimension },
            h("thead", null, h("tr", null, [byRepo ? "Repository" : "Group", "Input", "Output", "Total tokens", "Est. USD", API_LABEL, "Sessions", "API calls"].map(apiHead))),
            h("tbody", null, sumRows.map(function (v) {
              const t = v.group.totals;
              const repo = repoOf(v);
              return h("tr", { key: v.group.id, "data-group-id": v.group.id },
                h("td", null, str(v.group.label), repo && repo.text ? h("div", { className: "ud-muted ud-small", "data-repo-path": repo.count === 1 ? "single" : "multi" }, repo.text) : null),
                h(MetricCell, { metrics: t, field: "input_count" }),
                h(MetricCell, { metrics: t, field: "output_count" }),
                h(TotalTokensCell, { metrics: t }),
                h(MetricCell, { metrics: t, field: "estimated_usd", unit: "usd" }),
                h(ApiCell, { metrics: t, rowCount: v.group.row_ids.length }),
                h(MetricCell, { metrics: t, field: "sessions" }),
                h(MetricCell, { metrics: t, field: "api_calls" }));
            })))),
        byRepo && coverage ? h("p", { className: "ud-muted ud-small", "data-repo-coverage": String(coverage.pct) }, coverage.text) : null,
        h(GroupDurations, { views: views, depth: depthInfo }),
        byRepo ? h(RepoPaths, { views: views, depth: depthInfo }) : null,
        h("details", { className: "ud-table-wrap", "data-grouped-rows": "true", open: undefined },
          h("summary", null, "Daily rows by " + noun + " (" + mainRows.length + ")"),
          h(RowsTable, { rows: mainRows, providers: props.providers, withGroup: true, name: "grouped",
            groupLabel: function (r) { return str(groupOfRow[r.id] && groupOfRow[r.id].label); } })),
        otherView ? h("details", { className: "ud-table-wrap", "data-other-rows": "true" },
          h("summary", null, "Other — " + otherView.rows.length + " daily rows from the models rolled up into Other"),
          h(RowsTable, { rows: otherView.rows, providers: props.providers, name: "other" })) : null);
    }

    function UsageZone(props) {
      const st = props.res.state;
      const reload = props.res.reload;
      const providers = props.providers;
      const d = st.data;
      const mode = d ? d.mode : props.view;
      const dimension = d ? d.dimension : effectiveDimension(props.view, props.dimension);
      let content;
      if (st.status === "loading" && !st.data) {
        content = h(Skeleton, { rows: 5, label: "Loading usage analytics…", className: "ud-skel-chart" });
      } else if (st.status === "error" && !st.data) {
        content = h(ErrorCard, { what: "usage analytics", error: st.error, onRetry: reload });
      } else {
        const err = st.status === "error" ? h(ErrorCard, { key: "err", what: "usage analytics", error: st.error, onRetry: reload }) : null;
        let inner;
        if (d.series.length === 0) {
          const reasonKey = d.reason || "no_observations";
          inner = h("p", { className: "ud-empty-text", "data-chart-state": reasonKey },
            d.provider_id && reasonKey === "no_observations" ? "No observations for " + providerLabelOf(d.providers, d.provider_id) + " in this period." : REASON_TEXT[reasonKey]);
        } else if (d.dimension === "all") {
          inner = d.series.map(function (s, i) { return h(SeriesBlock, { key: str(s.id) || i, series: s, rows: d.rows, providers: d.providers }); });
        } else {
          inner = h(GroupedView, { key: [d.mode, d.dimension, d.window_days, d.provider_id].join("|"), data: d, providers: d.providers });
        }
        content = h("div", { "aria-busy": st.refreshing ? "true" : undefined }, err, h(CoverageNote, { data: d }), inner, h(TotalsBar, { totals: d.totals, rowCount: d.rows.length, depth: d.depth }),
          h(HeatmapCard, { depth: d.depth }), h(CacheCard, { depth: d.depth, providers: d.providers }),
          h(PricingNotes, { data: d }), h(UnpricedModels, { data: d }));
      }
      const options = dimensionOptions(mode);
      const selected = providers.filter(function (p) { return p.id === props.provider; })[0];
      return h("section", { className: "ud-zone", "aria-labelledby": "ud-h-usage", "data-zone": "usage", "data-mode": mode, "data-dimension": dimension },
        h("div", { className: "ud-zone-head" },
          h("h2", { id: "ud-h-usage" }, "Usage analytics"),
          st.data && (st.status === "error" || st.refreshing) ? h(StaleTag, { stale: true }) : null,
          h("div", { className: "ud-controls" },
            h("div", { role: "group", "aria-label": "Analytics dimension", className: "ud-seg", "data-dimension-toggle": dimension },
              options.map(function (o) {
                return h("button", {
                  key: o.id, type: "button", className: cx("ud-btn", "ud-seg-btn", dimension === o.id && "ud-seg-on"),
                  "aria-pressed": dimension === o.id ? "true" : "false", "data-dimension": o.id,
                  onClick: function () { props.onDimension(o.id); },
                }, o.label);
              })),
            h("div", { role: "group", "aria-label": "Time window", className: "ud-seg" },
              WINDOWS.map(function (w) {
                return h("button", {
                  key: w, type: "button", className: cx("ud-btn", "ud-seg-btn", props.window === w && "ud-seg-on"),
                  "aria-pressed": props.window === w ? "true" : "false", onClick: function () { props.onWindow(w); },
                }, w + " days");
              })),
            h("label", { className: "ud-select-label" },
              h("span", { className: "ud-small" }, "Provider"),
              h("select", {
                className: "ud-select", value: props.provider, "aria-label": "Provider filter",
                onChange: function (e) { props.onProvider(e.target.value); },
              },
                h("option", { value: "" }, "All providers"),
                providers.map(function (p) { return h("option", { key: p.id, value: p.id }, p.label); }))))),
        h("p", { className: "ud-muted ud-small ud-subtitle" },
          scopeCopy(mode) + (props.provider ? " Filtered to " + (selected ? selected.label : "the selected provider") + "." : "")),
        h("p", { className: "ud-muted ud-small", "data-attribution-limit": "true" }, COPY_ATTRIBUTION_LIMIT),
        content);
    }

    // --- Zone 3: health strip ---

    function HealthRow(props) {
      const e = props.entry;
      const meta = has(HEALTH_META, e.status) ? HEALTH_META[e.status] : HEALTH_META.unknown;
      const statusKey = has(HEALTH_META, e.status) ? e.status : "unknown";
      const metricsOk = e.metrics_available === true;
      let conf = asConfidence(e.confidence);
      if (!metricsOk && conf === "self_hosted") conf = "opaque"; // no metrics, no self_hosted claim
      const latency = num(e.latency_ms);
      const cpu = num(e.cpu_percent);
      const mem = num(e.memory_bytes);
      const reason = has(REASON_TEXT, e.reason) ? REASON_TEXT[e.reason] : null;
      const stale = e.stale === true || props.zoneStale;
      const de = isObj(e.error) ? deckErrorFrom({ error: e.error }) : null;
      const tip = "Source: " + (metricsOk ? "endpoint metrics and health probe" : "endpoint health probe only") +
        ". Scope: endpoint. Observed: " + fmtTime(e.checked_at) + ".";
      return h("li", { className: "ud-health-row", "data-endpoint-id": str(e.endpoint_id) || undefined, "data-status": statusKey },
        h("span", { className: "ud-status ud-status-" + statusKey },
          h("span", { className: "ud-dot-status", "aria-hidden": "true" }, meta.icon), " ", meta.text),
        h("span", { className: "ud-health-name" }, str(e.display_name) || str(e.provider_id) || "Endpoint"),
        h("span", { className: "ud-health-metrics" },
          h("span", { className: "ud-latency" }, latency === null ? "Latency —" : Math.round(latency) + " ms"),
          cpu !== null ? h("span", null, " · CPU " + (Math.round(cpu * 10) / 10) + "%") : null,
          mem !== null ? h("span", null, " · Memory " + fmtNumber(mem, "bytes")) : null),
        h(Badge, { conf: conf, tip: tip }),
        h("span", { className: "ud-muted ud-small" }, "Checked " + fmtTime(e.checked_at)),
        stale ? h(StaleTag, { stale: true }) : null,
        !metricsOk && !de ? h("span", { className: "ud-muted ud-small" }, "Metrics unavailable") : null,
        reason && !de ? h("span", { className: "ud-muted ud-small" }, reason) : null,
        e.reason === "box_offline" ? h("span", { className: "ud-muted ud-small", "data-field": "last-seen" }, lastSeenText(e)) : null,
        props.machine && e.profile_owned === true
          ? h("span", { className: "ud-badge ud-muted ud-small", "data-profile-owned": "true", title: PROFILE_OWNED_NOTE }, PROFILE_OWNED_BADGE) : null,
        props.machine && statusKey === "unknown" && e.reason === "source_unavailable" && !de && e.profile_owned !== true
          ? h("span", { className: "ud-muted ud-small", "data-machine-only-note": "true", title: MACHINE_ONLY_NOTE }, "Not probed") : null,
        de ? h("span", { className: "ud-small ud-health-err", role: "note" }, "⚠ " + de.message) : null);
    }

    function HealthZone(props) {
      const st = props.res.state;
      const machine = props.view === "machine";
      const reload = props.res.reload;
      let content;
      if (st.status === "loading" && !st.data) {
        content = h(Skeleton, { rows: 2, label: "Loading endpoint health…" });
      } else if (st.status === "error" && !st.data) {
        content = h(ErrorCard, { what: "endpoint health", error: st.error, onRetry: reload });
      } else {
        const entries = st.data.entries;
        const stale = st.status === "error" || st.refreshing;
        const err = st.status === "error" ? h(ErrorCard, { key: "err", what: "endpoint health", error: st.error, onRetry: reload }) : null;
        content = h("div", { "aria-busy": st.refreshing ? "true" : undefined }, err,
          entries.length === 0
            ? h("p", { className: "ud-empty-text" }, machine ? "No self-hosted endpoints detected on this machine." : "No self-hosted endpoints detected in this profile.")
            : h("ul", { className: "ud-health" }, entries.map(function (e, i) {
              return h(HealthRow, { key: str(e.endpoint_id) || i, entry: e, zoneStale: stale, machine: machine });
            })));
      }
      return h("section", { className: "ud-zone", "aria-labelledby": "ud-h-health", "data-zone": "health" },
        h("div", { className: "ud-zone-head" },
          h("h2", { id: "ud-h-health" }, "Self-hosted health"),
          st.data && (st.status === "error" || st.refreshing) ? h(StaleTag, { stale: true }) : null),
        content);
    }

    // --- Page ---

    function UsageDeckPage() {
      const profileKey = currentProfileKey();
      const [windowDays, setWindowDays] = useState(DEFAULT_WINDOW);
      const [provider, setProvider] = useState("");
      const [view, setView] = useState(DEFAULT_VIEW);
      const [dimension, setDimension] = useState(DEFAULT_DIMENSION);
      // Last provider options reported by the analytics (historical providers + Unknown): kept while the next
      // answer loads so the selector never flickers empty.
      const [providerOptions, setProviderOptions] = useState([]);

      // Machine -> profile resets a by-profile dimension to All before anything is fetched.
      const onView = function (v) {
        setView(v);
        setDimension(function (d) { return effectiveDimension(v, d); });
      };
      const effDimension = effectiveDimension(view, dimension);

      const subs = useResource("subs|" + profileKey + "|" + view, function (signal) {
        return getJSON("/subscriptions?view=" + view, signal).then(validateSubscriptions);
      });
      const health = useResource("health|" + profileKey + "|" + view, function (signal) {
        return getJSON("/health?view=" + view, signal).then(validateHealth);
      });

      const usageKey = "usage|" + profileKey + "|" + windowDays + "|" + provider + "|" + view + "|" + effDimension;
      const usage = useResource(usageKey, function (signal) {
        const q = "?mode=" + view + "&dimension=" + effDimension + "&window=" + windowDays +
          (provider ? "&provider=" + encodeURIComponent(provider) : "");
        return getJSON("/usage" + q, signal, USAGE_SCHEMA_VERSION).then(function (env) {
          return validateUsage(env, windowDays, { provider: provider, mode: view, dimension: effDimension });
        });
      });

      const usageData = usage.state.data;
      useEffect(function () {
        if (usageData) setProviderOptions(usageData.providers);
      }, [usageData]);
      const providers = useMemo(function () {
        return provider && !providerOptions.some(function (p) { return p.id === provider; })
          ? providerOptions.concat([{ id: provider, label: provider }]) : providerOptions;
      }, [providerOptions, provider]);

      // A provider the server no longer knows (window / mode change) falls back to All providers.
      const ust = usage.state;
      const providerGone = !!provider && ust.status === "error" && !!ust.error && ust.error.code === "provider_not_found";
      useEffect(function () { if (providerGone) setProvider(""); }, [providerGone]);

      const refreshAll = function () { subs.reload(); usage.reload(); health.reload(); };

      const sst = subs.state;
      const phase = sst.status === "ready" && sst.data.cards.length > 0 ? "connected" : "first_run";
      const busy = sst.refreshing || usage.state.refreshing || health.state.refreshing;
      const lastChecked = Math.max(sst.loadedAt, usage.state.loadedAt, health.state.loadedAt);

      return h("div", { className: "usage-deck", "data-phase": phase, "data-window": windowDays, role: "region", "aria-label": "UsageDeck" },
        h("header", { className: "ud-header" },
          h("div", null,
            h("h1", { className: "ud-title" }, "UsageDeck"),
            h("p", { className: "ud-muted ud-small" }, "Detected subscriptions across this machine's Hermes profiles, session usage for the selected profile, and self-hosted health. Read-only; no credentials are requested.")),
          h("div", { className: "ud-header-actions" },
            lastChecked ? h("span", { className: "ud-muted ud-small" }, "Updated " + fmtTime(new Date(lastChecked).toISOString().replace(/\.\d+Z$/, "Z"))) : null,
            h("button", {
              type: "button", className: "ud-btn", onClick: refreshAll, disabled: busy || undefined,
              title: "Re-requests all three zones. The server may answer from its 30-second cache, so values can be up to 30 seconds old.",
            }, busy ? "Refreshing…" : "Refresh"))),
        h(SubscriptionsZone, { res: subs, provider: provider, view: view, onView: onView }),
        h(UsageZone, {
          res: usage, window: windowDays, onWindow: setWindowDays, provider: provider, onProvider: setProvider, providers: providers,
          view: view, dimension: effDimension, onDimension: setDimension,
        }),
        h(HealthZone, { res: health, view: view }),
        h("div", { className: "ud-sr", role: "status", "aria-live": "polite" },
          busy ? "Refreshing UsageDeck data" : "UsageDeck data up to date"));
    }

    return { UsageDeckPage: UsageDeckPage, components: { Badge: Badge, SubscriptionCard: SubscriptionCard, LineChart: LineChart, HealthRow: HealthRow } };
  }

  // ---------------------------------------------------------------------------
  // Bootstrap
  // ---------------------------------------------------------------------------

  const pure = {
    API_BASE: API_BASE,
    effectiveConfidence: effectiveConfidence,
    meterConfidence: meterConfidence,
    cardConfidence: cardConfidence,
    cardView: cardView,
    isMeasured: isMeasured,
    progressRatio: progressRatio,
    fmtNumber: fmtNumber,
    fmtTime: fmtTime,
    splitSegments: splitSegments,
    classifyError: classifyError,
    authInfo: authInfo,
    unwrapEnvelope: unwrapEnvelope,
    niceMax: niceMax,
    holderNames: holderNames,
    opaqueNote: opaqueNote,
    machineOnlyNote: machineOnlyNote,
    validateUsage: validateUsage,
    dimensionOptions: dimensionOptions,
    effectiveDimension: effectiveDimension,
    totalTokens: totalTokens,
    fmtMetric: fmtMetric,
    scopeCopy: scopeCopy,
    groupViews: groupViews,
    apiView: apiView,
    fmtApi: fmtApi,
    picoToUsd: picoToUsd,
    apiFlags: apiFlags,
    apiTitle: apiTitle,
    unpricedModels: unpricedModels,
    pricingNotes: pricingNotes,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = Object.assign({ createUsageDeck: createUsageDeck }, pure);
  }

  const SDK = root && root.__HERMES_PLUGIN_SDK__;
  if (SDK && root.__HERMES_PLUGINS__ && typeof root.__HERMES_PLUGINS__.register === "function") {
    root.__HERMES_PLUGINS__.register("usage-deck", createUsageDeck(SDK).UsageDeckPage);
  }
})(typeof window !== "undefined" ? window : globalThis);
