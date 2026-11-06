# scratch_home — synthetic acceptance fixture

Deterministic, **entirely invented** Hermes home used by the actual-host acceptance harness
(`tests/acceptance/host_acceptance.py`) and by `tests/test_acceptance_fixture.py`. Nothing here is, was
derived from, or may ever be replaced by a real credential, hostname or account.

| File | Contents |
|---|---|
| `auth.json` | Root credential pool, 3 cloud accounts: `nous` (`syn-nous-1`), `openrouter` (`syn-or-1`), `openai-codex` (`syn-codex-1`, an *unsigned* JWT with `iss: synthetic.invalid` and only `plan_type: plus`) |
| `config.yaml.template` | 2 self-hosted endpoints: primary provider `ollama` (no credential, no `/metrics`) and custom provider `gpu-box` (has `/metrics`). `@@GPU_BASE_URL@@` / `@@OLLAMA_BASE_URL@@` are replaced with the harness's loopback fakes; the Nous keepalive thread is disabled so the host never refreshes OAuth or rewrites `auth.json` |
| `profiles/work/` | A secondary profile with its own openrouter row, to exercise profile-first pool shadowing (per provider, never combined) and the host's profile gate |

Seeded-secret design (all matched by the harness's leak scan, except the short one):

* long sentinels (`UDSYN-…`, `sk-or-v1-udsyn…`, the JWT and its segments) — must never appear in any response or log;
* `agent_key: "el"` — a 2-character private secret that is a substring of canonical wire keys (`label`) and enum
  values (`self_hosted`): responses must keep the exact schema while still masking it in free text;
* `label` on the nous row duplicates its access token — a secret under a generic field name.

What this fixture can and cannot show: it exercises the plugin through the real host with **synthetic** data
(an in-process TLS-terminating proxy answers `openrouter.ai` with an invented key payload). It makes **no claim
about any live provider pool**, real subscription, or real browser rendering.

Run: `python tests/acceptance/host_acceptance.py` (see the script's docstring and the repository README).
