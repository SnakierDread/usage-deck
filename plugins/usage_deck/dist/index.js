/**
 * UsageDeck — Hermes dashboard plugin (frontend).
 *
 * One screen, three independent zones:
 *   1. Subscriptions grid   GET /api/plugins/usage-deck/subscriptions?view=machine|profile
 *   2. Usage analytics      GET /api/plugins/usage-deck/usage?window=7|30|90[&provider=<id>]&view=...
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
    chatgpt_wham_usage: "exact",
    none: "opaque",
  };
  const SOURCE_LABEL = {
    openrouter_key: "OpenRouter key endpoint",
    session_analytics: "Hermes session analytics",
    prometheus: "Endpoint metrics",
    anthropic_headers: "Anthropic unified rate-limit headers captured from the last call",
    chatgpt_wham_usage: "ChatGPT workspace spend control (wham/usage)",
    none: "No verified source",
  };
  // Provider-specific, fixed local explanations for an opaque card that shows no meter (DESIGN.md §9).
  // Codex: source-aware (§9.2). The superseded blanket "never read" explanation is gone: the route is
  // ratified and read, so an opaque card means this account returned nothing this meter can honestly show.
  const OPAQUE_NOTES = {
    "openai-codex": "No Codex spend meter for this account: UsageDeck reads only the ChatGPT workspace spend control, and this account has none it can show right now (no usable credential, an unrecognised or expired reading, or access was not permitted). Nothing is estimated. Any plan shown is an unverified claim from the credential.",
    anthropic: "No Anthropic session/weekly meter: no unified rate-limit headers are captured for this credential. Hermes does not record them today (recon t_7dce2222), so meters appear only after a call's headers have been captured. Nothing is estimated.",
  };
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
  };
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

  /** Validate the envelope (DESIGN.md §3) and return `data`, or throw {uiError}. */
  function unwrapEnvelope(resp) {
    if (!isObj(resp)) throw malformed("not an object");
    if (resp.ok === false) {
      const de = deckErrorFrom(resp);
      if (de) throw { uiError: Object.assign({ kind: "deck", status: null }, de) };
      throw malformed("failure without error");
    }
    if (resp.ok !== true) throw malformed("missing ok flag");
    if (!isObj(resp.meta) || resp.meta.schema_version !== 1) throw malformed("unsupported schema version");
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

  function validateUsage(env, windowDays) {
    const d = env.data;
    if (d.window_days !== windowDays) throw malformed("window mismatch");
    if (!Array.isArray(d.series)) throw malformed("series");
    return {
      window_days: d.window_days,
      provider_id: str(d.provider_id),
      series: d.series.filter(isObj),
      reason: has(REASON_TEXT, d.reason) ? d.reason : null,
      meta: env.meta,
    };
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

  /** Recon-cited explanation for an opaque card of a provider that could expose usage, or null. */
  function opaqueNote(card) {
    if (!isObj(card) || card.kind === "self_hosted") return null;
    if (cardView(card) !== "unavailable") return null;
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

    function getJSON(path, signal) {
      const init = signal ? { signal: signal } : undefined;
      return SDK.fetchJSON(API_BASE + path, init).then(unwrapEnvelope);
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
      const fromSpend = m.source === "chatgpt_wham_usage";
      const resetsAt = str(m.resets_at);
      const tip = "Source: " + (SOURCE_LABEL[m.source] || SOURCE_LABEL.none) +
        ". Scope: " + (SCOPE_LABEL[m.scope] || "unknown") +
        ". Observed: " + fmtTime(m.observed_at) + "." +
        (resetsAt ? " Reset reported by the provider: " + fmtTime(resetsAt) + "." : "") +
        (fromHeaders ? " Value as of that call, not live; it is never extrapolated." : "") +
        (fromSpend ? " Credits reported for this seat's workspace spend limit, as of the last refresh; not USD, not messages, not a fleet total." : "");
      const parts = [];
      parts.push(h("div", { key: "head", className: "ud-meter-head" },
        h("span", { className: "ud-meter-label" }, str(m.label) || "Meter"),
        h(Badge, { conf: conf, tip: tip })));
      if (fromHeaders) {
        // Utilization of a rolling window: used share + provider-reported reset; no invented remaining.
        parts.push(h("dl", { key: "vals", className: "ud-vals" },
          h("div", null, h("dt", null, "Used"), h("dd", null, value === null ? "Not reported" : fmtNumber(value, unit))),
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
          h("p", { className: "ud-muted ud-small" },
            card.kind === "self_hosted"
              ? "Endpoint health appears in the health strip below."
              : "Session analytics only — profile-wide usage appears in the chart below, not as this account's allowance."),
          note ? h("p", { className: "ud-muted ud-small", "data-opaque-note": str(card.provider_id) }, note) : null);
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
            h(Badge, { conf: conf, tip: "Primary source for this card. Checked: " + fmtTime(card.checked_at) + "." + (note && conf === "opaque" ? " " + note : "") }))),
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
            ? data.cards.filter(function (c) { return str(c.provider_id) === props.provider; })
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
            ? "Machine view: the global Hermes home plus every profile's credential pool, merged when they are the same account (JWT account identity). Each card lists the profiles that hold it."
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

    function SeriesBlock(props) {
      const s = props.series;
      const conf = effectiveConfidence(s.confidence, SOURCE_CEILING[s.source] || "opaque");
      const pts = Array.isArray(s.points) ? s.points : [];
      const tip = "Source: Hermes session analytics. Scope: " + (s.scope === "profile" ? "this Hermes profile" : "provider-attributed") +
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
            h("thead", null, h("tr", null, ["Day", "Input", "Output", "Est. USD", "Sessions", "API calls"].map(function (c) { return h("th", { key: c, scope: "col" }, c); }))),
            h("tbody", null, sorted.map(function (p) {
              return h("tr", { key: p.day },
                h("td", null, p.day),
                h("td", null, fmtNumber(p.input_count, "count")),
                h("td", null, fmtNumber(p.output_count, "count")),
                h("td", null, fmtNumber(p.estimated_usd, "usd")),
                h("td", null, fmtNumber(p.sessions, "count")),
                h("td", null, fmtNumber(p.api_calls, "count")));
            })))));
    }

    function UsageZone(props) {
      const st = props.res.state;
      const reload = props.res.reload;
      const providers = props.providers;
      let content;
      if (st.status === "loading" && !st.data) {
        content = h(Skeleton, { rows: 5, label: "Loading usage analytics…", className: "ud-skel-chart" });
      } else if (st.status === "error" && !st.data) {
        content = h(ErrorCard, { what: "usage analytics", error: st.error, onRetry: reload });
      } else {
        const d = st.data;
        const err = st.status === "error" ? h(ErrorCard, { key: "err", what: "usage analytics", error: st.error, onRetry: reload }) : null;
        let inner;
        if (d.series.length === 0) {
          const reasonKey = d.reason || (props.provider ? "attribution_unavailable" : "no_observations");
          inner = h("p", { className: "ud-empty-text", "data-chart-state": reasonKey }, REASON_TEXT[reasonKey]);
        } else {
          inner = d.series.map(function (s, i) { return h(SeriesBlock, { key: str(s.id) || i, series: s }); });
        }
        content = h("div", { "aria-busy": st.refreshing ? "true" : undefined }, err, inner);
      }
      return h("section", { className: "ud-zone", "aria-labelledby": "ud-h-usage", "data-zone": "usage" },
        h("div", { className: "ud-zone-head" },
          h("h2", { id: "ud-h-usage" }, "Usage analytics"),
          st.data && (st.status === "error" || st.refreshing) ? h(StaleTag, { stale: true }) : null,
          h("div", { className: "ud-controls" },
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
                h("option", { value: "" }, "All (this profile)"),
                providers.map(function (p) { return h("option", { key: p.id, value: p.id }, p.label); }))))),
        h("p", { className: "ud-muted ud-small ud-subtitle" },
          props.provider
            ? "Filtered to " + (providers.filter(function (p) { return p.id === props.provider; })[0] || { label: "provider" }).label + ". Session analytics carry no per-provider attribution."
            : "This Hermes profile only — not subscription allowance."),
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
              return h(HealthRow, { key: str(e.endpoint_id) || i, entry: e, zoneStale: stale });
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

      const subs = useResource("subs|" + profileKey + "|" + view, function (signal) {
        return getJSON("/subscriptions?view=" + view, signal).then(validateSubscriptions);
      });
      const health = useResource("health|" + profileKey + "|" + view, function (signal) {
        return getJSON("/health?view=" + view, signal).then(validateHealth);
      });

      const providers = useMemo(function () {
        const seen = {};
        const out = [];
        const cards = subs.state.data ? subs.state.data.cards : [];
        cards.forEach(function (c) {
          const id = str(c.provider_id);
          if (id && !has(seen, id)) { seen[id] = true; out.push({ id: id, label: str(c.provider_label) || id }); }
        });
        return out;
      }, [subs.state.data]);

      // A provider that vanished from the inventory (profile switch) falls back to profile-wide.
      const providerValid = provider === "" || providers.some(function (p) { return p.id === provider; });
      useEffect(function () {
        if (!providerValid && subs.state.status === "ready") setProvider("");
      }, [providerValid, subs.state.status]);
      const effProvider = providerValid || subs.state.status !== "ready" ? provider : "";

      const usage = useResource("usage|" + profileKey + "|" + windowDays + "|" + effProvider + "|" + view, function (signal) {
        const q = "?window=" + windowDays + (effProvider ? "&provider=" + encodeURIComponent(effProvider) : "") + "&view=" + view;
        return getJSON("/usage" + q, signal).then(function (env) { return validateUsage(env, windowDays); });
      });

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
        h(SubscriptionsZone, { res: subs, provider: effProvider, view: view, onView: setView }),
        h(UsageZone, { res: usage, window: windowDays, onWindow: setWindowDays, provider: effProvider, onProvider: setProvider, providers: providers }),
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
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = Object.assign({ createUsageDeck: createUsageDeck }, pure);
  }

  const SDK = root && root.__HERMES_PLUGIN_SDK__;
  if (SDK && root.__HERMES_PLUGINS__ && typeof root.__HERMES_PLUGINS__.register === "function") {
    root.__HERMES_PLUGINS__.register("usage-deck", createUsageDeck(SDK).UsageDeckPage);
  }
})(typeof window !== "undefined" ? window : globalThis);
