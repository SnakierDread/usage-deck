# UsageDeck

A Hermes dashboard plugin that shows, on one screen, which model providers a Hermes profile is connected to, what usage is known about them, and whether self-hosted endpoints are healthy. Every number carries a confidence badge so you can see how far to trust it.

Pilot project of the Snakier Hermes fleet. Design contract: [`DESIGN.md`](DESIGN.md).

## What you get

One tab, **UsageDeck** (path `/usage-deck`, placed after *Models*), with three independent zones. A failure in one zone does not blank the others.

1. **Subscriptions grid (top).** One card per detected provider account, so two `openai-codex` accounts show as two cards. Each card shows the provider, an `Account N` label, the plan (only if the credential claims one, marked unverified), the usage meters the provider genuinely exposes, and a JSON drawer with the exact sanitized data behind the card.
2. **Usage analytics (middle).** Token and spend time series from this profile's own Hermes session accounting, with a 7 / 30 / 90 day picker and a provider picker.
3. **Self-hosted health strip (bottom).** One row per self-hosted endpoint (custom or Ollama-style `base_url`): healthy / degraded / offline / unknown, latency, and CPU and memory when the endpoint exposes `/metrics`. The strip is empty if you have no self-hosted endpoints.

### Confidence badges

| Badge | Meaning |
| --- | --- |
| **Exact** | A directly reported number for a known scope and period (for example an OpenRouter key's usage and limit). It is a key balance, not a subscription percentage. |
| **Estimated** | Computed from this profile's local session accounting, or incomplete coverage. Never an invoice or an allowance. |
| **Opaque** | The provider exposes no verified measurement. No quota, percentage or reset time is shown, and a missing value is never displayed as zero. A credential or plan claim cannot raise this. |
| **Self-hosted** | Operational metrics from a configured endpoint's valid `/metrics`. This is a *source category*, not a higher accuracy tier than Exact or Estimated. |

A meter is never shown at higher confidence than its source. Unknown providers still get a card, marked Opaque, plus session analytics.

### Safety properties

- **No credential collection.** UsageDeck never asks you to type or paste a key. First run is an auto-detected inventory of what Hermes already knows. An empty inventory just says "No providers detected in this profile".
- **Read-only.** It reads the profile's `auth.json`, `config.yaml` and the dashboard's existing analytics, plus a few read-only `GET` requests to a provider or to your own endpoint (OpenRouter key info, `/metrics`, `/v1/models`). It does not change configuration or call anything that spends money.
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

- `GET /api/plugins/usage-deck/subscriptions`
- `GET /api/plugins/usage-deck/usage?window=7|30|90&provider=<id>`
- `GET /api/plugins/usage-deck/health`

These sit behind the dashboard's session check, so a bare `curl` or a new browser tab returns `401 Unauthorized`. Read the data through the UI.

### Update or remove

```sh
git pull                                   # in your clone
hermes plugins install "file://$(pwd)#plugins/usage_deck" --force --no-enable
hermes plugins remove usage-deck           # uninstall
```

After a reinstall, run `hermes plugins list --plain --no-bundled` and re-enable if it shows `disabled`.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `hermes plugins install ./plugins/usage_deck` fails | Unsupported shorthand. Use the quoted `file://$(pwd)#plugins/usage_deck` form from step 2, run from the repo root. |
| Install fails with a clone or authentication error | The repo is private. Confirm that `git clone https://github.com/SnakierDread/usage-deck.git` works on this machine first. |
| Installed copy is old, or misses your edits | Only committed files are installed. Commit, then reinstall with `--force`. |
| No **UsageDeck** tab | Run `hermes plugins list --plain --no-bundled`. If it says `disabled`, run `hermes plugins enable usage-deck`. Then restart `hermes dashboard` and reload the browser. |
| Tab opens but says "No providers detected in this profile" | Normal when the selected profile has no credentials or custom endpoints. Switch to a profile that has them, or configure a provider in Hermes. UsageDeck will not ask for keys. |
| A card is **Opaque** with no numbers | The provider exposes no usage or quota endpoint that UsageDeck can verify. This is deliberate: it shows no number rather than a guess. |
| A self-hosted row is **offline** or **unknown** | The endpoint did not answer in time, or its `base_url` is invalid. Check the URL in your Hermes config and that it serves `/metrics` and `/v1/models`. Missing CPU or memory means the endpoint does not report them. |
| Usage chart is empty or shows an error but the cards load | Session analytics are unavailable or empty for that profile. The zones are independent, so subscriptions and health are unaffected. |
| `curl` on `/api/plugins/usage-deck/...` returns 401 | Expected. See "Where the data comes from". |
| `hermes plugins validate` or `hermes plugins doctor` reports `no register() function` | UsageDeck is a dashboard-only plugin and registers no agent tools or hooks, so the agent-side capability probe reports this. In testing the dashboard still discovered the plugin and served its API. |

## Development

Run these from the repository root. They use the Python standard library, plus Node with React and jsdom taken from the Hermes checkout for the frontend tests (test-only, never plugin dependencies).

```sh
python -m unittest discover -s plugins/usage_deck/tests -t .     # backend
tests/run_frontend.sh                                            # frontend + packaging
python scripts/stage_dashboard.py --check --require-backend      # staged dashboard/ is in sync
```

`plugins/usage_deck/dashboard/` is generated. Never edit it by hand. After changing backend or frontend sources, rebuild it with `python scripts/stage_dashboard.py --require-backend` and commit the result. Commit messages use `feat:`, `fix:`, `test:` or `docs:` prefixes.

### Actual-host acceptance (real Hermes dashboard, synthetic data)

`plugins/usage_deck/tests/test_plugin_api.py` mounts the router on a standalone FastAPI app. The harness below
instead drives the **real dashboard host** (DESIGN.md §3): it installs the *committed* plugin
(`hermes plugins install file://<repo>#plugins/usage_deck`) into an isolated scratch Hermes home built from
`plugins/usage_deck/tests/fixtures/scratch_home/` (3 synthetic cloud accounts + 2 self-hosted endpoints), starts
`hermes dashboard`, and checks `/api/plugins/usage-deck/{subscriptions,usage,health}` through the host's own
auth, plugin-consent and profile gates: exact schema, source-bound confidence labels, seeded-secret masking,
unknown-provider / malformed-store / empty-home cases and config/auth immutability.

```sh
git status --short --untracked-files=no            # must be clean: the harness installs the committed HEAD
python tests/acceptance/host_acceptance.py          # exit 0 only if every check passes; --keep keeps logs + report.json
python tests/acceptance/host_acceptance.py --self-check   # offline: fixture + validators only
```

Needs the `hermes` CLI, `git` and `openssl` on `PATH` and a managed temp directory (`$HERMES_KANBAN_WORKSPACE`,
`$TMPDIR` or `--workdir`). Everything runs in a clean child environment whose `HOME` and `HERMES_HOME` live inside that
directory, so Hermes' ancestry-based global-root lookup can never reach the real `~/.hermes`. All HTTPS from the host
is forced through a loopback CONNECT proxy that refuses everything except `openrouter.ai:443`, which a throw-away CA
answers with an invented payload (the host's own update checks are refused and logged, not sent). Results
describe the synthetic fixture only: they are not evidence about any live pool, subscription or browser rendering.
