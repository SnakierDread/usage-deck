# Provider Usage/Quota Endpoint Inventory

This document provides a cited inventory of what usage/quota data is available from each provider in the fleet.

## Providers Enumerated from auth.json + config.yaml

From `auth.json` credential_pool:
- `custom:pgx` - http://192.168.0.118:8000/v1 (api_key)
- `openai-codex` - https://chatgpt.com/backend-api/codex (oauth)
- `anthropic` - https://api.anthropic.com (oauth)

From `config.yaml` custom_providers:
- PGX - http://192.168.0.118:8000/v1
- snakier-llama-swap - http://192.168.0.188:8080/v1
- qwen3.6:35BA3B - http://192.168.0.210:8081/v1

## Usage/Quota Endpoint Table

| provider_id | endpoint | method | auth_shape | response_keys | confidence | notes |
|-------------|----------|--------|------------|---------------|------------|-------|
| openai-codex | https://api.openai.com/v1/dashboard/billing/usage | GET | Bearer token | error (invalid_api_key) | opaque | Dashboard endpoints are for platform site/browser use only, not programmatic. Requires org admin API key for programmatic access via Admin API "get costs" endpoint. |
| openai-codex | https://api.openai.com/v1/dashboard/billing/credit_grants | GET | Bearer token | error (invalid_api_key) | opaque | Same as above - dashboard endpoints not for programmatic use. |
| openai-codex | https://api.openai.com/v1/dashboard/billing/subscription | GET | Bearer token | error (invalid_api_key) | opaque | Same as above - dashboard endpoints not for programmatic use. |
| openai-codex | https://chatgpt.com/backend-api/codex/v1/dashboard/billing/usage | GET | OAuth bearer | invalid_jwt error | opaque | Codex backend returns 401 with invalid JWT for programmatic billing calls. |
| anthropic | https://api.anthropic.com/v1/organizations/usage | GET | x-api-key header | authentication_error (invalid x-api-key) | session analytics only | Old endpoint format; migrated to Usage and Cost Analytics API via client SDK (`client.beta.organization.analytics.usage.list()`). |
| anthropic | https://api.anthropic.com/v1/organizations/usage_report/messages | GET | x-api-key header | authentication_error | session analytics only | Deprecated usage report endpoint; use `client.beta.organization.analytics.usage.list()` and `client.beta.organization.analytics.cost.list()`. |
| anthropic | Spend Limits API (SDK) | N/A (SDK) | OAuth/API key | effective_limits, period_to_date_spend, amount | exact | SDK endpoints: `client.beta.organization.spend_limits.effective.list()`, `client.beta.organization.spend_limits.set()`. |
| openrouter | https://openrouter.ai/api/v1/credits | GET | Bearer token | total_credits, total_usage | exact | Returns total credits purchased and used. Docs say management key required but works with ordinary keys. |
| openrouter | https://openrouter.ai/api/v1/key | GET | Bearer token | limit_remaining, usage, usage_daily, usage_weekly, usage_monthly, budgets, effective_budget | exact | Per-key usage, limits, and budget info. Also returns rate-limit metadata on 402/403 errors. |
| openrouter | https://openrouter.ai/api/v1/keys | GET | Bearer token | Missing Authentication header (401) | opaque | Key management endpoint; requires management key. |
| nous | https://api.nousresearch.com/api/usage | GET | Bearer token | (empty response) | opaque | No documented usage endpoint; returns empty or 404. |
| nous | https://portal.nousresearch.com/api/usage | GET | Bearer token | HTML error page | opaque | Portal returns Next.js error page; no programmatic usage API documented. |
| pgx (custom:pgx) | http://192.168.0.118:8000/v1/models | GET | api_key in config | vLLM models list response | exact | vLLM-compatible endpoint; returns model metadata but no usage/quota data. |
| pgx (custom:pgx) | http://192.168.0.118:8000/metrics | GET | none | Prometheus metrics format | exact | Exposes Prometheus /metrics endpoint with python_gc_objects_collected_total, etc. No model-specific usage metrics. |
| pgx (custom:pgx) | http://192.168.0.118:8000/api/tags | GET | none | {"detail":"Not Found"} | opaque | Not an ollama endpoint; returns 404. |
| snakier-llama-swap | http://192.168.0.188:8080/v1/models | GET | none | connection failed (000) | opaque | Endpoint not responding; connection timeout/failure. |
| qwen3.6:35BA3B | http://192.168.0.210:8081/v1/models | GET | none | llama-swap models list response | exact | llama-swap endpoint; returns model metadata including context_length, capabilities, etc. |
| qwen3.6:35BA3B | http://192.168.0.210:8081/metrics | GET | none | Prometheus metrics (llamaswap_cpu_util_percent, llamaswap_memory_total_bytes) | exact | Exposes Prometheus /metrics endpoint with CPU and memory utilization per core. No model token usage metrics. |
| qwen3.6:35BA3B | http://192.168.0.210:8081/api/tags | GET | none | 404 page not found | opaque | Not an ollama endpoint; returns 404. |

## Raw curl Examples (secrets masked)

### OpenAI Billing Endpoints
```bash
# Usage endpoint
curl -s https://api.openai.com/v1/dashboard/billing/usage \
  -H "Authorization: Bearer sk-test"

# Response: {"error": {"message": "Incorrect API key provided: sk-test...", "type": "invalid_request_error", "code": "invalid_api_key"}}

# Credit grants endpoint
curl -s https://api.openai.com/v1/dashboard/billing/credit_grants \
  -H "Authorization: Bearer sk-test"

# Response: {"error": {"message": "Incorrect API key provided: sk-test...", "type": "invalid_request_error", "code": "invalid_api_key"}}
```

### Anthropic Usage Endpoint
```bash
curl -s https://api.anthropic.com/v1/organizations/usage \
  -H "x-api-key: sk-test" \
  -H "anthropic-version: 2023-06-01"

# Response: {"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"},"request_id":"req_..."}
```

### OpenRouter Credits Endpoint
```bash
curl -s https://openrouter.ai/api/v1/credits \
  -H "Authorization: Bearer sk-test"

# Response (with valid key): {"data": {"total_credits": 100.5, "total_usage": 25.75}}
# Response (with invalid key): {"error":{"message":"Missing Authentication header","code":401}}
```

### OpenRouter Key Endpoint
```bash
curl -s https://openrouter.ai/api/v1/key \
  -H "Authorization: Bearer sk-test"

# Response (with valid key): Returns per-key usage, limit_remaining, usage_daily/weekly/monthly, budgets, effective_budget
# Response (with invalid key): {"error":{"message":"Missing Authentication header","code":401}}
```

### Self-hosted vLLM /metrics Endpoint
```bash
curl -s http://192.168.0.118:8000/metrics | head -c 500

# Response: "# HELP python_gc_objects_collected_total Objects collected during gc\n# TYPE python_gc_objects_collected_total counter\npython_gc_objects_collected_total{generation=\"0\"} 2.736651e+06..."
```

### Self-hosted llama-swap /metrics Endpoint
```bash
curl -s http://192.168.0.210:8081/metrics | head -c 500

# Response: "# HELP llamaswap_cpu_util_percent CPU utilization per core (0-100)\n# TYPE llamaswap_cpu_util_percent gauge\nllamaswap_cpu_util_percent{core=\"0\"} 100..."
```

## Summary by Provider Type

### First-party Cloud Providers (OpenAI, Anthropic)
- **OpenAI**: Dashboard billing endpoints (`/v1/dashboard/billing/*`) are for platform/browser use only. Programmatic access requires org admin API key and the Admin API "get costs" endpoint.
- **Anthropic**: Old `/v1/organizations/usage` endpoint returns auth errors. Usage and cost data is available via SDK analytics endpoints (`client.beta.organization.analytics.usage.list()`, `client.beta.organization.analytics.cost.list()`). Spend limits available via `client.beta.organization.spend_limits.effective.list()`.

### Aggregator Providers (OpenRouter)
- **OpenRouter**: `GET /api/v1/credits` returns `total_credits` and `total_usage`. `GET /api/v1/key` returns per-key usage (`usage`, `usage_daily`, `usage_weekly`, `usage_monthly`), limits (`limit`, `limit_remaining`), and budgets.

### Self-hosted Endpoints (vLLM, llama-swap)
- **vLLM** (http://192.168.0.118:8000/v1): Exposes Prometheus `/metrics` endpoint with Python GC metrics but no model-specific token usage metrics. No ollama `/api/tags` endpoint.
- **llama-swap** (http://192.168.0.210:8081/v1): Exposes Prometheus `/metrics` endpoint with CPU and memory utilization (`llamaswap_cpu_util_percent`, `llamaswap_memory_total_bytes`). No model token usage metrics. No ollama `/api/tags` endpoint.

### Opaque Providers
- **Nous**: No documented or working usage/quota API endpoints found. Returns empty responses or HTML error pages.
- **snakier-llama-swap** (http://192.168.0.188:8080/v1): Endpoint not responding; connection timeout/failure.
