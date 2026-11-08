# UsageDeck

A Hermes dashboard plugin that shows, on one screen, which model providers a Hermes profile is connected to, what usage is known about them, and whether self-hosted endpoints are healthy. Every number carries a confidence badge so you can see how far to trust it.

Pilot project of the Snakier Hermes fleet. Design contract: [`DESIGN.md`](DESIGN.md).

## What you get

One tab, **UsageDeck** (path `/usage-deck`, placed after *Models*), with three independent zones. A failure in one zone does not blank the others.

1. **Subscriptions grid (top).** One card per detected provider account, so two `openai-codex` accounts show as two cards. Each card shows the provider, an `Account N` label, the plan (only if the credential claims one, marked unverified), the usage meters the provider genuinely exposes, and a JSON drawer with the exact sanitized data behind the card. A **Whole machine / This profile** toggle picks the scope:
   - **Whole machine** (default): the union of the global `auth.json` and every `profiles/*/auth.json` credential pool. The same account held in several homes is **one** card, titled with the profiles that hold it, for example `OpenAI Codex [go] — verifier`. Only `openai-codex` logins are matched, by their JWT account id **and** user id together, so a refreshed token for the same seat still merges while two seats of one workspace do not (an account id alone, without a user id, never merges anything). Every other credential stays one card per home: identical public pool row ids in different homes are *not* proof of one account, so you may see the same account twice rather than have one home's data shown on another's card. UsageDeck never compares or hashes secrets to merge. This is a display heuristic, not proof of identity or of shared billing, and nothing is summed across cards.
   - **This profile**: the dashboard's selected profile with the global fallback per provider, exactly as before. Cards are not merged across profiles.
2. **Usage analytics (middle).** Token and spend time series from this profile's own Hermes session accounting, with a 7 / 30 / 90 day picker and a provider picker.
3. **Self-hosted health strip (bottom).** One row per self-hosted endpoint (custom or Ollama-style `base_url`): healthy / degraded / offline / unknown, latency, and CPU and memory when the endpoint exposes `/metrics`. The strip is empty if you have no self-hosted endpoints.

### Confidence badges

| Badge | Meaning |
| --- | --- |
| **Exact** | A directly reported number for a known scope and period: an OpenRouter key's usage and limit (a key balance, not a subscription percentage), a Codex seat's workspace spend limit in credits, or an Anthropic subscription's session (5-hour) and weekly (7-day) utilization with its provider-reported reset, taken from captured rate-limit headers and shown **as of the last call**, never extrapolated. |
| **Estimated** | Computed from this profile's local session accounting, or incomplete coverage. Never an invoice or an allowance. |
| **Opaque** | The provider exposes no verified measurement. No quota, percentage or reset time is shown, and a missing value is never displayed as zero. A credential or plan claim cannot raise this. |
| **Self-hosted** | Operational metrics from a configured endpoint's valid `/metrics`. This is a *source category*, not a higher accuracy tier than Exact or Estimated. |

A meter is never shown at higher confidence than its source. Unknown providers still get a card, marked Opaque, plus session analytics.

### Subscription meters: what is and is not shown

| Provider | What you see | Why |
| --- | --- | --- |
| Anthropic | Session (5h) and Weekly (7d) **Exact** meters, each with `Used`, `Resets`, `As of` and a bar, **only when a usable capture exists** for that credential. Otherwise **Opaque**: "No usable capture; enable the optional observer and use this account normally." | The values come from Anthropic's `anthropic-ratelimit-unified-*` response headers of requests Hermes **already makes**. UsageDeck never makes a request to get them (no inference call, no spend) and a meter can only appear after an ordinary request on that account returned eligible headers. Capturing is **opt-in** (see below), so on a fresh install every Anthropic card is Opaque. A captured window whose reset time has passed is dropped, not shown as zero. |
| OpenAI Codex | **Exact** `Workspace spend limit (monthly)` meter in **credits** (`Used`, `Limit`, `Remaining`, `Resets`, `As of`, a bar when both used and limit are known), only when your account reports a workspace spend control. Otherwise **Opaque** (or an error card on a rejected credential) with a fixed explanation. | One read-only `GET https://chatgpt.com/backend-api/wham/usage` per refresh, authorised by the Codex access token Hermes already stores for the selected profile (in the Whole machine view an account held only by other profiles is never fetched: no request, no credential) and routed by that same token's account/residency claims (DESIGN.md §9.2, `docs/decisions/codex-wham-usage.md`). Credits are not USD, messages or a fleet total, and the figure is for that one seat's workspace spend limit. Other response fields (credits balance, models, rate limits, email, ids) are ignored. No refresh, no retry, no other endpoint. A `401` shows an `auth_required` error and never rewrites your credentials. The plan (`[go]`) stays an unverified claim from the credential. |
| OpenRouter | **Exact** key spend/limit. | Documented `GET /api/v1/key`. |
| Nous, unknown providers, self-hosted | Opaque (self-hosted: health strip). | No verified usage source. |

### Optional Anthropic observer (opt-in, off by default)

Anthropic only reports its session and weekly utilization in the headers of ordinary requests. If you want UsageDeck to show them, a small agent-side observer can keep the latest values locally. It is **off unless you turn it on**; installing or enabling the plugin and opening the dashboard capture nothing.

```yaml
# <profile>/config.yaml  (the profile whose Hermes agent makes the Anthropic requests)
usage_deck:
  capture_anthropic_headers: true
```

- **Whose setting counts:** the profile that made the request (not the profile that owns the credential, and not the profile the dashboard is showing). Absent, `false` or any non-boolean value means off.
- **Install and enable the plugin for that profile too.** Plugin consent is per profile (`hermes plugins enable usage-deck` with that profile's `HERMES_HOME`); the observer only runs in a profile where UsageDeck is enabled *and* the setting is `true`.
- **Needs a Hermes with the `provider_ratelimit_headers` hook.** On other Hermes versions the observer simply does nothing. It adds no request, no retry and no credential refresh; it only records headers from requests that were happening anyway, for native `api.anthropic.com` OAuth pool rows with a public row id.
- **What is stored, and where:** only the twelve `anthropic-ratelimit-unified-*` headers (validated numbers and short status words), the public pool row id, which profile's auth store owns that row, and the time the response arrived, in `<hermes root>/plugin-data/usage-deck/anthropic-ratelimit.json` (`0600` in a `0700` directory; at most 256 entries / 256 KiB). No tokens, cookies, request ids, bodies or e-mail addresses.
- **Manual deletion:** delete that file (or the `plugin-data/usage-deck/` directory) at any time; the next opted-in request starts a new one. Turning the setting off stops further writes but leaves existing observations, which UsageDeck keeps displaying only while their reset time is in the future. A capture file from the pre-release v1 format (no holder) is refused and never migrated: delete it to start capturing.
- Your auth store, profile config and environment files are never modified; the dashboard itself only reads the capture file.

### Safety properties

- **No credential collection.** UsageDeck never asks you to type or paste a key. First run is an auto-detected inventory of what Hermes already knows. An empty inventory just says "No providers detected in this profile".
- **Read-only.** It reads the profile's `auth.json`, `config.yaml` and the dashboard's existing analytics, plus a few read-only `GET` requests to a provider or to your own endpoint (OpenRouter key info, the Codex workspace spend control at `chatgpt.com/backend-api/wham/usage`, `/metrics`, `/v1/models`). It does not change configuration or call anything that spends money. In **Whole machine** view it also reads the `auth.json`, the custom-endpoint entries of `config.yaml` and the `.env` variables those entries name (never analytics, never any other file or variable) of every profile under the same Hermes root (real directories with valid profile names only, never symlinks, never a symlinked `profiles/` folder, at most 64), so anyone who can open this dashboard as the machine owner can see which accounts every profile on this machine holds (never their tokens). This is a same-owner convenience view, **not tenant isolation**; a dashboard principal restricted to one profile is always shown the profile view. In **Whole machine** view (and only there, DESIGN.md §13, user-ratified 2026-11-07) a Codex account whose credential lives in another profile is read with that profile's own token, and a self-hosted endpoint held only by another profile gets one `/metrics` scrape, authenticated only with that profile's own `key_env` value from that profile's own `.env` when it has one (never a models/tags request, never any other credential): read-only `GET`s from a fixed allowlist, credentials used in memory for that one request and never written, logged or returned. Everywhere else (the profile view, any profile's own agent) a profile uses only its own effective credentials and config. Accounts of other providers held only by other profiles (OpenRouter, Nous, Anthropic without a capture) are listed as inventory ("No live source for this account") and never probed. It also reads, never writes, the optional header-capture file.
- **Secrets are masked.** Every value that reaches a response, log or the JSON drawer is sanitized. Fields whose names look secret become `[REDACTED]`, and every secret value found in your credentials is scrubbed from displayed text, whatever its length.
- **No extra dependencies.** Python standard library plus the dashboard's own FastAPI and React. Nothing else is installed with it.

## Install

### Prerequisites

- Hermes Agent with the `hermes` command on your `PATH` (developed and tested on v0.22.7). Check with `hermes --version`.
- `git`. The installer clones from git.
- Read access to the repository `https://github.com/SnakierDread/usage-deck` (it is private), signed in to GitHub on this machine so that `git clone` works.
- At least one provider or self-hosted endpoint configured in the Hermes profile you will view. With none, the tab loads but shows the empty state.

### 1. Get the code

```sh
git clone https://github.com/SnakierDread/usage-deck.git
cd usage-deck
git checkout main   # or the branch or commit you were asked to install
git status          # should be clean: only committed files are installed
```

The installer clones the repository's committed state. Uncommitted edits in the folder are not installed.

### 2. Install the plugin

Run this from the repository root. The form is `file://<absolute path to this repo>#plugins/usage_deck`, where the part after `#` is the plugin subfolder. Keep the quotes, otherwise your shell treats `#` as a comment.

```sh
hermes plugins install "file://$(pwd)#plugins/usage_deck" --no-enable
```

Hermes warns that this is a custom, unreviewed, local source. That is expected for a file install. The plugin lands in `$HERMES_HOME/plugins/usage-deck` (by default `~/.hermes/plugins/usage-deck`).

The shorthand `hermes plugins install ./plugins/usage_deck` is **not supported**. Use the `file://...#plugins/usage_deck` form.

To try it without touching your real Hermes home, run the same commands with `HERMES_HOME=/some/scratch/dir` set. To pin an exact commit, add `--ref <40-character-sha>`.

### 3. Enable it

Plugins are opt-in. An installed plugin does nothing until enabled.

```sh
hermes plugins enable usage-deck
hermes plugins list --plain --no-bundled   # expect a line: enabled  user  0.1.0  usage-deck
```

### 4. Open the dashboard

```sh
hermes dashboard
```

Open the URL it prints (default `http://127.0.0.1:9119`; `--port` changes it, `--no-open` stops it launching a browser). If a dashboard was already running when you installed, stop it and start it again so it rediscovers plugins.

Click **UsageDeck** in the tab bar, or go to `/usage-deck`. The view follows the dashboard's selected profile and uses the dashboard's own sign-in. There is no separate login.

### Where the data comes from

The plugin's API is served by the dashboard under `/api/plugins/usage-deck/`:

- `GET /api/plugins/usage-deck/subscriptions?view=machine|profile`
- `GET /api/plugins/usage-deck/usage?window=7|30|90&provider=<id>&view=machine|profile`
- `GET /api/plugins/usage-deck/health?view=machine|profile`

`view` defaults to `machine`. It selects the inventory for `subscriptions` and `health`; for `usage` it only decides which inventory validates `provider=` (the chart is always this profile's own session analytics).

These sit behind the dashboard's session check, so a bare `curl` or a new browser tab returns `401 Unauthorized`. Read the data through the UI.

### Update or remove

```sh
git pull                                   # in your clone
hermes plugins install "file://$(pwd)#plugins/usage_deck" --force --no-enable
hermes plugins remove usage-deck           # uninstall
```

After a reinstall, run `hermes plugins list --plain --no-bundled` and re-enable if it shows `disabled`.

### Desktop app half (optional)

The package also carries a half for the native Hermes desktop app: `plugins/usage_deck/desktop/plugin.js` (a **Desktop** target plus a `manifest.json` that only describes it). The desktop app copies it into its own `$HERMES_HOME/desktop-plugins/usage-deck/` on the machine where the app runs. The desktop half shows nothing without the agent half (steps 2–3) on the gateway the app is connected to. There are three ways to get it, from most to least integrated. All three leave the web dashboard tab above untouched.

1. **Catalog.** Not available: UsageDeck is a private repository and is not listed in any plugin catalog. Use route 2.
2. **Install from Git** (recommended). In the app: **Capabilities → Plugins → Install from Git**, keep the **Desktop** target checked, and enter the **package folder** (canonical: it installs both halves as one package):

   ```text
   https://github.com/SnakierDread/usage-deck/tree/main/plugins/usage_deck
   ```

   (`SnakierDread/usage-deck/plugins/usage_deck` and `https://github.com/SnakierDread/usage-deck.git#plugins/usage_deck` resolve to the same thing.) Review the dialog: it should list the package as `usage-deck` with a Desktop half. Confirm, then **Rescan**, then enable the Desktop half in **Plugins** (it ships disabled). This machine needs `git` and read access to the private repo (the app clones non-interactively, so sign in to GitHub with a credential helper first). To update, install again with **Force**.

   The **bare repository URL** is also accepted, so a copied repo link no longer dead-ends:

   ```text
   https://github.com/SnakierDread/usage-deck
   ```

   It is the **desktop half only**. The repository root carries a `plugin.js` (a generated, byte-identical copy of `plugins/usage_deck/desktop/plugin.js`) and nothing else the installer recognises, so the dialog lists a Desktop half and no agent half, and the app publishes the whole repository tree (about 2 MB; `.git` and `__pycache__` left out) as the desktop plugin `usage-deck`. Nothing is installed on the gateway: the page shows *backend unavailable* until you also install the agent half (steps 2–3 above, or the folder URL). Once the agent half is installed the app recognises the folder as that package's desktop half (the files are byte-identical) and pairs them; if the pairing does not happen, remove the `usage-deck` desktop plugin and install the folder URL with **Force**. Prefer the folder URL: one install, both halves, update/uninstall tracked.
3. **Disk door** (manual standalone copy, no git needed). Copy exactly one file:

   ```sh
   mkdir -p "$HERMES_HOME/desktop-plugins/usage-deck"      # default: ~/.hermes/desktop-plugins/usage-deck
   cp plugins/usage_deck/desktop/plugin.js "$HERMES_HOME/desktop-plugins/usage-deck/plugin.js"
   ```

   The app loads it within seconds and hot-reloads each save. Do not copy `manifest.json` or write a `.hermes-package.json` marker. A folder with a `plugin.js` and no marker is a hand-installed standalone plugin: the app never overwrites it, so it has no update or uninstall tracking. To switch to route 2 later, delete that folder first. Remote gateway: this is the route that works when the agent half lives on another machine, since the app never reads a remote filesystem.

Layout rules the installer enforces, kept true by `tests/packaging/test_package_manifests.py`: no `plugin.js` at the package root, a repository-root `plugin.js` that is a byte-exact copy of `plugins/usage_deck/desktop/plugin.js` (the bare-URL entry above; it wins the entry search, so the whole repository is published as the desktop half; it cannot be a re-export shim because the app's loader resolves only `@hermes/plugin-sdk`, `react` and `react/jsx-runtime`), no root `plugin.yaml`/`plugin.json`/`__init__.py`/`dashboard/` (a root `plugin.json` would make the probe report an agent half that the agent installer then copies as the whole repository and rejects without an Agent Plugins schema), no symlinks anywhere, and a tree far under the 64 MB cap. `manifest.json` (repository root) and `plugins/usage_deck/desktop/manifest.json` are metadata only: `name`, `version`, entry path. No Hermes surface reads them as an agent or dashboard manifest, because the agent half is detected from `plugin.yaml` plus `__init__.py` or `dashboard/manifest.json`.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `hermes plugins install ./plugins/usage_deck` fails | Unsupported shorthand. Use the quoted `file://$(pwd)#plugins/usage_deck` form from step 2, run from the repo root. |
| Install fails with a clone or authentication error | The repo is private. Confirm that `git clone https://github.com/SnakierDread/usage-deck.git` works on this machine first. |
| Desktop **Install from Git** says *No agent or desktop plugin artifacts found* | The app predates the repository-root `plugin.js`, or the URL is not this repository. Enter the package folder: `https://github.com/SnakierDread/usage-deck/tree/main/plugins/usage_deck`. See "Desktop app half". |
| Desktop **Install from Git** with the bare repository URL installed a half that shows *backend unavailable* | The bare URL installs the desktop half only. Install the agent half too (steps 2–3, or the folder URL). |
| Desktop **Install from Git** still fails while staging the desktop half | Use the disk door (copy `plugins/usage_deck/desktop/plugin.js` to `$HERMES_HOME/desktop-plugins/usage-deck/plugin.js`) and report the exact error text with your app version. See "Desktop app half". |
| Installed copy is old, or misses your edits | Only committed files are installed. Commit, then reinstall with `--force`. |
| No **UsageDeck** tab | Run `hermes plugins list --plain --no-bundled`. If it says `disabled`, run `hermes plugins enable usage-deck`. Then restart `hermes dashboard` and reload the browser. |
| Tab opens but says "No providers detected in this profile" | Normal when the selected profile has no credentials or custom endpoints. Switch to a profile that has them, or configure a provider in Hermes. UsageDeck will not ask for keys. |
| A card is **Opaque** with no numbers | The provider exposes no usage or quota endpoint that UsageDeck can verify. This is deliberate: it shows no number rather than a guess. The card's tooltip says which case applies. |
| An Anthropic card has no Session/Weekly meters | No usable capture exists for that credential: the optional observer is off (the default), your Hermes lacks the `provider_ratelimit_headers` hook, the account has not made an ordinary request since, the captured windows already reset, or a v1/malformed capture file is being refused. See "Optional Anthropic observer". |
| An account appears twice in **Whole machine** | Only Codex logins with a complete JWT account + user identity merge across homes; anything else is deliberately kept per home, because equal row ids in different homes prove nothing. It never merges by comparing secrets. |
| A profile's accounts are missing in **Whole machine** | That profile's `auth.json` is unreadable or malformed and was skipped (a path-free warning is logged), or the profile directory is a symlink or has an invalid name. |
| A self-hosted row is **offline** or **unknown** | The endpoint did not answer in time, or its `base_url` is invalid. Check the URL in your Hermes config and that it serves `/metrics` and `/v1/models`. Missing CPU or memory means the endpoint does not report them. |
| Usage chart is empty or shows an error but the cards load | Session analytics are unavailable or empty for that profile. The zones are independent, so subscriptions and health are unaffected. |
| `curl` on `/api/plugins/usage-deck/...` returns 401 | Expected. See "Where the data comes from". |
| An OpenRouter or Nous account in **Whole machine** says "No live source for this account" | It is held only by another profile and has no documented read-only usage probe that may use another profile's credential (only the Codex spend control and an endpoint's `/metrics` do). UsageDeck lists it from that profile's `auth.json`. Open that profile's dashboard to check it live. |

## Development

Run these from the repository root. They use the Python standard library, plus Node with React and jsdom taken from the Hermes checkout for the frontend tests (test-only, never plugin dependencies).

```sh
python -m unittest discover -s plugins/usage_deck/tests -t .     # backend (machine view, capture store, observer, header meters)
tests/run_frontend.sh                                            # frontend + packaging
python scripts/stage_dashboard.py --check --require-backend      # staged dashboard/ is in sync
python scripts/stage_root_entry.py --check                       # root plugin.js is in sync with desktop/plugin.js
```

Header data in tests is **synthetic**: `plugins/usage_deck/tests/fixtures/synthetic_anthropic_capture.json` is invented, flagged `"synthetic": true` (the production reader refuses such files) and was not captured from any real response. The machine-view fixtures are temporary multi-profile homes built in `tests/test_machine_view.py` / `tests/test_machine_scope.py` plus the committed `scratch_home/` (profiles `work` and `verifier`). `tests/test_capture_store.py` covers the schema-2 writer (true multi-process contention, lock timeout, symlink/oversize/future/synthetic/out-of-order cases) and `tests/test_observer.py` the opt-in observer; `tests/test_core_seam.py` runs the REAL Hermes core hook dispatch into the loaded plugin when `USAGE_DECK_CORE_CHECKOUT` points at a Hermes checkout that carries the `provider_ratelimit_headers` hook (skipped otherwise). None of this is a live provider observation.

`plugins/usage_deck/dashboard/` is generated. Never edit it by hand. After changing backend or frontend sources, rebuild it with `python scripts/stage_dashboard.py --require-backend` and commit the result. Commit messages use `feat:`, `fix:`, `test:` or `docs:` prefixes.

### Actual-host acceptance (real Hermes dashboard, synthetic data)

`plugins/usage_deck/tests/test_plugin_api.py` mounts the router on a standalone FastAPI app. The harness below
instead drives the **real dashboard host** (DESIGN.md §3): it installs the *committed* plugin
(`hermes plugins install file://<repo>#plugins/usage_deck`) into an isolated scratch Hermes home built from
`plugins/usage_deck/tests/fixtures/scratch_home/` (3 synthetic cloud accounts + 2 self-hosted endpoints, plus profiles `work` and `verifier` for the machine view and a harness-written synthetic Anthropic header capture), starts
`hermes dashboard`, and checks `/api/plugins/usage-deck/{subscriptions,usage,health}` through the host's own
auth, plugin-consent and profile gates: exact schema, source-bound confidence labels, seeded-secret masking,
unknown-provider / malformed-store / empty-home cases and config/auth immutability.

```sh
git status --short --untracked-files=no            # must be clean: the harness installs the committed HEAD
python tests/acceptance/host_acceptance.py          # exit 0 only if every check passes; --keep keeps logs + report.json
python tests/acceptance/host_acceptance.py --self-check   # offline: fixture + validators only
python -m unittest discover -s tests/acceptance           # offline: interpreter-selection tests (stub launcher)
```

Helper subprocesses that import Hermes modules (session seeding, the `hermes_constants` isolation probe) run on one
`HERMES_PYTHON`, resolved once: `--hermes-python PATH`, then `$HERMES_PYTHON`, then auto-detected by asking the `hermes`
launcher what it runs on (its activated venv interpreter), and only then `python3` on `PATH`. A bare `python3` lacks
Hermes's dependency closure (`ModuleNotFoundError: ruamel`); the harness refuses to start if the chosen interpreter
cannot import `hermes_constants`/`hermes_state`. Any other runner that imports Hermes (e.g. the core-seam test behind
`USAGE_DECK_CORE_CHECKOUT`) should reuse `tests/acceptance/hermes_python.py` rather than `sys.executable`.

Needs the `hermes` CLI, `git` and `openssl` on `PATH` and a managed temp directory (`$HERMES_KANBAN_WORKSPACE`,
`$TMPDIR` or `--workdir`). Everything runs in a clean child environment whose `HOME` and `HERMES_HOME` live inside that
directory, so Hermes' ancestry-based global-root lookup can never reach the real `~/.hermes`. All HTTPS from the host
is forced through a loopback CONNECT proxy that refuses everything except `openrouter.ai:443` and `chatgpt.com:443` (the one
ratified Codex route), which a throw-away CA answers with invented payloads (the host's own update checks are refused and logged, not sent). Results
describe the synthetic fixture only: they are not evidence about any live pool, subscription or browser rendering.
