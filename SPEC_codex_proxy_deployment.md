# Spec: Deploy CLIProxyAPI to Run Claude Code with Codex/OpenAI Backend

Date: 2026-03-12

## Goal

Run Claude Code on a VPS, using an OpenAI Codex OAuth subscription as the backend instead of an Anthropic API key. This is achieved by deploying [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) as a translation proxy between Claude Code's API format and OpenAI's Codex API.

## Architecture

```
Claude Code CLI (on VPS)
    │  sends Claude API requests (/v1/messages)
    ▼
CLIProxyAPI proxy (Docker, localhost:8317)
    │  translates Claude format → OpenAI Responses format
    │  maps model names (e.g. claude-sonnet-4-6 → gpt-5.4)
    ▼
OpenAI Codex API (authenticated via OAuth, Plus subscription)
```

## What is CLIProxyAPI

A Go-based universal AI API proxy that translates between different AI provider formats (Claude, OpenAI/Codex, Gemini, etc.). Key capabilities:

- Bi-directional request/response translation between providers
- OAuth and API key authentication for all providers
- Load balancing across multiple credentials
- Model name aliasing (map any model name to any backend model)
- Hot-reload configuration (file watcher, no restart needed)
- Extended thinking support across providers

## VPS Details

- **IP**: (redacted)
- **OS**: Ubuntu 24.04 LTS
- **RAM**: 4 GB
- **User**: root (SSH key auth configured)
- **Pre-installed**: Node.js v22, Git, Claude Code v2.1.63+

## Deployment Steps Performed

### 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
```

### 2. Clone and start CLIProxyAPI

```bash
cd /opt
git clone https://github.com/router-for-me/CLIProxyAPI.git
cd CLIProxyAPI
```

### 3. Create config.yaml

```yaml
host: ""
port: 8317

auth-dir: "~/.cli-proxy-api"

api-keys:
  - "claude-code-proxy-key"

debug: true
request-retry: 3

quota-exceeded:
  switch-project: true
  switch-preview-model: true

routing:
  strategy: "round-robin"

oauth-model-alias:
  codex:
    - name: "gpt-5.4"
      alias: "claude-sonnet-4-6"
    - name: "gpt-5.4"
      alias: "claude-opus-4-6"
    - name: "gpt-5.3-codex"
      alias: "claude-sonnet-4-5-20250929"
    - name: "gpt-5.1-codex"
      alias: "claude-haiku-4-5-20251001"
```

Key config sections:
- `api-keys` — authentication token Claude Code uses to connect to the proxy
- `oauth-model-alias.codex` — maps Claude model names that Claude Code requests to actual OpenAI/Codex model names

### 4. Start proxy with Docker Compose

```bash
# In tmux session "proxy"
docker compose up
```

The pre-built image `eceasy/cli-proxy-api:latest` is pulled automatically. Listens on port 8317.

### 5. Authenticate Codex OAuth (one-time)

```bash
docker exec -it cli-proxy-api ./CLIProxyAPI -codex-device-login
```

This uses the device code flow:
1. Shows a URL (https://auth.openai.com/codex/device) and a device code
2. User opens URL in browser, enters code, authenticates with OpenAI account
3. OAuth tokens saved to `/root/.cli-proxy-api/codex-{email}-plus.json` inside the container

### 6. Pre-configure Claude Code to skip onboarding

**~/.claude.json** — skip onboarding + approve custom API key:
```python
config["hasCompletedOnboarding"] = True
config["customApiKeyResponses"] = {"approved": [api_key[-20:]]}
```

**~/.claude/settings.json** — skip dangerous mode prompt:
```json
{"skipDangerousModePermissionPrompt": true}
```

### 7. Configure Claude Code environment

Added to `~/.bashrc`:
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8317
export ANTHROPIC_API_KEY=claude-code-proxy-key
```

### 8. Run Claude Code

```bash
claude
```

Claude Code connects to the local proxy, which translates requests and forwards to OpenAI via Codex OAuth.

## Model Mapping

| Claude Code requests       | Proxy routes to   |
|----------------------------|-------------------|
| `claude-sonnet-4-6`        | `gpt-5.4`         |
| `claude-opus-4-6`          | `gpt-5.4`         |
| `claude-sonnet-4-5-20250929` | `gpt-5.3-codex` |
| `claude-haiku-4-5-20251001`  | `gpt-5.1-codex` |

## Files on VPS

| Path | Purpose |
|------|---------|
| `/opt/CLIProxyAPI/config.yaml` | Proxy configuration (hot-reloads on change) |
| `/opt/CLIProxyAPI/docker-compose.yml` | Docker Compose service definition |
| `~/.cli-proxy-api/` | OAuth token storage (mounted as Docker volume) |
| `~/.claude.json` | Claude Code onboarding config |
| `~/.claude/settings.json` | Claude Code settings |

## tmux Sessions

| Session | Purpose |
|---------|---------|
| `proxy` | Running CLIProxyAPI Docker container |

## Key Decisions

1. **Docker over native build** — Go 1.26 required by the project caused long compile times on the VPS. Docker pulls a pre-built image instantly.
2. **Codex OAuth over API key** — Uses the OpenAI Plus subscription quota rather than burning API credits from a pay-per-use API key.
3. **oauth-model-alias** — The critical config that makes this work. Claude Code sends requests for `claude-sonnet-4-6` but the proxy only has Codex models. Aliases map Claude model names to available Codex models transparently.
4. **Claude Code onboarding bypass** — Without pre-configuring `~/.claude.json`, Claude Code tries to authenticate against the real Anthropic platform. The `hasCompletedOnboarding` + `customApiKeyResponses` trick (from ccclaw) skips this entirely.

## Relevance to claude-telegram

This proxy setup can be combined with claude-telegram to create a Telegram-controlled Claude Code instance that runs on OpenAI/Codex models:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8317 ANTHROPIC_API_KEY=claude-code-proxy-key python claude-telegram.py
```

This would let you control Claude Code from Telegram, with all requests powered by your Codex subscription.
