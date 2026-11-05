PURPOSE
UsageDeck: a Hermes dashboard plugin showing connected models/providers, subscription usage, and token analytics across any Hermes install — redeployable, discovery-driven, no baked-in credentials.

CONTEXT (grounding, verified)
- Dashboard plugin shape: plugins/<name>/{manifest.json, plugin_api.py, dist/} — study plugins/kanban in the Hermes source tree first.
- Existing backend to reuse: GET /api/analytics/usage and /api/analytics/models already exist in the dashboard; hermes insights proves per-profile session accounting.
- Auth model: profile-first, global-fallback auth.json resolution; credential pools (multiple accounts per provider, e.g. openai-codex business+go) with plan identity in JWT claims.
- Repo: https://github.com/SnakierDread/usage-deck (private; local clone /root/projects/usage-deck; default branch main).

DECISIONS (binding; stamp into child cards)
- Plugin dir: usage_deck; backend routes: /api/usage-deck/*.
- Data sources: auth.json + config.yaml + existing /api/analytics/* — read-only; DTOs mask everything secret-shaped.
- Adapter registry keyed by provider id: nous, openrouter, openai-codex, anthropic, self-hosted(custom:*/ollama-style base_urls), unknown → "session analytics only" fallback. Never crash on an unknown provider.
- Confidence taxonomy (mandatory in every data path AND its UI component): exact | estimated | opaque | self-hosted. A meter is never shown at higher confidence than its source.
- UI: single screen, three zones — subscriptions grid (top), usage analytics time-series w/ 7-30-90 picker (middle), self-hosted health strip (bottom). Theme-following via Hermes dashboard plugin slots + design tokens. Status-first cards; JSON drawer per card for raw transparency. First-run connect screen = auto-detected inventory, never a credential wall.
- No runtime deps beyond Hermes stdlib + the dashboard's own build toolchain.
- Naming: card titles "[plug] <type>: <outcome>"; branches = kanban task id; commits conventional (feat:/fix:/test:/docs:).

MVP WORK ITEMS (for decomposition)
1. recon — live inventory of what Portal/OpenRouter/OpenAI/Anthropic + self-hosted endpoints expose for usage/quota TODAY, cited (probe from this box). Feeds item 2.
2. design — adapter contract, data model, UI states (empty/error/opaque), confidence rules. Deliverable: DESIGN.md in repo. Owner: architect.
3. impl(backend) — adapter registry, /api/usage-deck/* routes, provider probes, health strip data. Parent: design. Owner: builder.
4. impl(frontend) — plugin manifest, UI per DESIGN.md. Parent: design; sibling-independent of backend via contract. Owner: builder.
5. qa — contract tests against a scratch HERMES_HOME fixture + live probes of this machine's pool. Parent: both impls. Owner: verifier.
6. review — PRs for items 3-5 merged only after PASS. Owner: architect.

DONE MEANS
All PRs green+merged to main; `hermes plugins install <local path>` on this machine shows this fleet's 3 subscription accounts + 2 self-hosted endpoints with correct confidence labels; README has a zero-context install section.

VERIFY
On a scratch HERMES_HOME copy: plugin loads, lists that home's providers, API responses contain no token/secret substrings, opaque providers render honestly.
