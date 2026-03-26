# Ideas from OpenClaw — and Why Claude Code Already Does It Better

OpenClaw implements several useful agent infrastructure features from scratch.
This document shows that **every one of them** maps to a native Claude Code feature
(or a thin wrapper around one), meaning claude-telegram can adopt them with
minimal code — and get a better version than OpenClaw ships.

---

## 1. Heartbeat System

### What OpenClaw builds
A custom timer loop (default 30 min) that wakes the agent, runs a
`HEARTBEAT.md` prompt, prunes no-op replies, deduplicates within 24 h,
supports quiet-hours, and skips if a user request is in-flight.

### Claude Code equivalent: `/loop`
```
/loop 30m check HEARTBEAT.md and report if anything needs attention
```

That single line gives you:
- Recurring execution at any interval (seconds to days)
- Low-priority: fires between user turns, never interrupts
- 3-day auto-expiry (no stale timers)
- Up to 50 concurrent scheduled tasks per session
- Underlying `CronCreate` / `CronList` / `CronDelete` tools for programmatic control

**What claude-telegram needs to add:**
- Nothing for basic heartbeat — just inject `/loop` into the session prompt or CLAUDE.md.
- For deduplication / quiet-hours: a small hook script (~20 lines) on the `Stop` event
  that suppresses duplicate messages and checks wall-clock time.

**Why it's better:**
OpenClaw heartbeat is a single-purpose timer bolted onto a custom gateway.
`/loop` is a general scheduler the LLM itself controls — it can adjust its own
interval, cancel the loop, or create additional loops at runtime.

---

## 2. Cron Scheduler

### What OpenClaw builds
`at` (one-shot), `every` (interval), `cron` (full cron expressions with timezone),
payload injection, delivery modes (chat / webhook / internal), stagger windows,
failure alerts, job history tracking.

### Claude Code equivalent: `/loop` + `/schedule`

| Need | Tool | Scope |
|------|------|-------|
| Quick polling ("check build every 5 min") | `/loop 5m …` | Session-scoped |
| One-shot reminder ("at 3 pm push branch") | `/loop` with natural language | Session-scoped |
| Full cron expressions | `CronCreate` tool | Session-scoped |
| Durable recurring jobs (survive reboots) | `/schedule` | Cloud (Anthropic infra) |

`/schedule` runs on Anthropic infrastructure — no local machine needed,
survives reboots, has built-in history, and auto-creates PRs on `claude/*` branches.

**What claude-telegram needs to add:**
- Expose `/schedule` results to Telegram by forwarding the cloud task output.
- For stagger windows: already built in — `/loop` adds up to 15 min jitter on hourly tasks.
- For failure alerts: a `Stop` hook that checks exit status and sends a Telegram message.

**Why it's better:**
OpenClaw's cron is session-scoped only — if the gateway crashes, jobs are lost.
Claude Code offers *both* session-scoped (`/loop`) and durable cloud (`/schedule`),
plus the LLM can manage its own schedule at runtime.

---

## 3. Multi-Agent System

### What OpenClaw builds
Config-driven routing (channel → agent binding), per-agent isolation
(workspace, sessions, model, tools policy), depth-based roles
(main → orchestrator → leaf), subagent spawning with depth/count limits.

### Claude Code equivalent: Agent Teams + per-user Linux isolation

claude-telegram already has **multi-user isolation** via separate Linux users,
each with their own home directory, tmux session, CLAUDE.md, skills, and
permission policy. This is stronger than OpenClaw's in-process isolation.

For multi-agent within a single user: **Agent Teams** (v2.1.71+):
- Lead session spawns teammate sessions with independent context windows
- Teammates communicate directly (no lead bottleneck)
- Shared task list with dependencies
- `TeammateIdle` and `TaskCompleted` hooks for quality gates
- Worktree isolation per teammate (no file conflicts)

For routing: claude-telegram's router already matches Telegram users to
Linux sessions. Extending it to match by group ID or role is trivial
(~30 lines in `router.py`).

**What claude-telegram needs to add:**
- Expose Agent Teams to Telegram users (forward teammate status/results).
- Add group-ID / role-based routing in `router.py` (config-driven).

**Why it's better:**
OpenClaw isolates agents within a single process — a crash in one agent can
poison the gateway. claude-telegram isolates at the OS level (separate Linux
users + tmux sessions). Agent Teams add LLM-driven coordination on top,
with hooks for deterministic quality gates that OpenClaw lacks.

---

## 4. Subagent Orchestration

### What OpenClaw builds
Parallel fan-out, fan-in via `sessions_yield`, session introspection
(`sessions_list/history/status`), result auto-push with exponential backoff,
orphan recovery on gateway restart.

### Claude Code equivalent: Agent Teams + Background Agents + Worktrees

| OpenClaw feature | Claude Code native |
|---|---|
| Parallel fan-out | Agent Teams (N teammates spawned in one turn) |
| Fan-in / yield | `TeammateIdle` hook (push-based, fires on completion) |
| Session introspection | Shared task list (lead sees all task statuses) |
| Result announcement | Teammate → lead messaging (direct, no polling) |
| Orphan recovery | Worktree isolation (changes preserved on crash) |

**What claude-telegram needs to add:**
- Forward Agent Team task status to Telegram (status updates in chat).
- Orphan recovery: worktrees already preserve uncommitted work. Add a
  `SessionStart` hook that scans `.claude/worktrees/` for abandoned branches.

**Why it's better:**
OpenClaw children get *only* a task string (no parent context) — they start
blind. Claude Code teammates inherit CLAUDE.md and MCP servers, giving them
project awareness from turn one. The shared task list with dependencies is
richer than OpenClaw's flat fan-out/fan-in model.

---

## 5. Agent-to-Agent Messaging

### What OpenClaw builds
Opt-in `tools.agentToAgent` policy, LLM decides when to message peers.

### Claude Code equivalent: `claude-peers-mcp`

The [claude-peers-mcp](file:///Users/tom/meiji/others/claude-peers-mcp) server
(already available locally) provides exactly this:

| Feature | claude-peers-mcp |
|---|---|
| Peer discovery | `list_peers` tool (scope: machine / directory / repo) |
| Send message | `send_message` tool (instant delivery via `claude/channel`) |
| Status sharing | `set_summary` (auto-generated or manual) |
| Health tracking | 15s heartbeat, auto-cleanup of dead peers |

Architecture: broker daemon (localhost:7899, SQLite) + per-session MCP server.
Messages arrive via channel push — instant, not polled.

**What claude-telegram needs to add:**
- Install `claude-peers-mcp` as an MCP server for each user session.
- Done. Each user's Claude can already discover and message other users' sessions.

**Why it's better:**
OpenClaw's agent-to-agent messaging is internal to one gateway process.
`claude-peers-mcp` works across *any* Claude Code instances on the machine —
including standalone terminal sessions, IDE sessions, and claude-telegram
users. The broker architecture (SQLite + HTTP) is more resilient than
in-process message passing.

---

## 6. System Event Queue

### What OpenClaw builds
In-memory queue of human-readable events (cron results, webhook triggers,
channel health). Max 20, deduplicated, prepended to agent's next prompt.

### Claude Code equivalent: Hooks + Channels

Claude Code's **hook system** already fires events for every lifecycle moment:
`FileChanged`, `TaskCompleted`, `TeammateIdle`, `CwdChanged`, `ConfigChange`, etc.

**Channels** push external events (Telegram messages, webhooks) directly into
the running session — no queue needed, the LLM sees them in real time.

For batched delivery (like OpenClaw's "prepend to next prompt"):
- A `PreToolUse` or `UserPromptSubmit` hook can read from a local event file
  and inject accumulated events as context.

**What claude-telegram needs to add:**
- A small event-collector script (hook on `Notification` / `Stop`) that
  appends events to `~/.claude/events.log`.
- A `UserPromptSubmit` hook that reads + clears the log and prepends it.
- ~40 lines of shell script total.

**Why it's better:**
OpenClaw's event queue is a custom in-memory structure that dies with the
gateway. Claude Code hooks are config-driven, persistent, and composable —
you can chain hooks, filter by event type, and use HTTP hooks to forward
events to external systems.

---

## 7. Channel Health Monitor

### What OpenClaw builds
Background daemon (every 5 min) that detects dead channels and auto-restarts.
Rate-limited (max 10 restarts/hour). Prevents the bot from going offline.

### Claude Code equivalent: already built into claude-telegram

claude-telegram's router already:
- Monitors each user's tmux session
- Detects socket disconnection
- Supports `/restart` command to respawn sessions
- Persists session IDs for `--resume` across restarts

**What claude-telegram needs to add:**
- A `/loop 5m` health check or a simple cron job outside Claude:
  `*/5 * * * * /opt/claude-telegram/healthcheck.sh`
- The script checks if the router tmux session is alive, restarts if not.
- ~15 lines of bash.

**Why it's better:**
OpenClaw needs a custom daemon because it built everything from scratch.
claude-telegram already has OS-level process isolation (tmux) with built-in
reconnection. A trivial cron job covers the gap.

---

## 8. Hooks / Webhooks

### What OpenClaw builds
HTTP webhooks trigger agent turns, Gmail watcher polls and triggers handlers,
wake modes ("now" vs "next-heartbeat").

### Claude Code equivalent: Hooks (command + HTTP) + Channels

Claude Code hooks support:
- **Command hooks**: shell scripts triggered by 20+ event types
- **HTTP hooks**: POST JSON to any URL on any event
- **Prompt hooks**: single LLM call (Haiku) for yes/no decisions
- **Agent hooks**: multi-turn subagent for complex verification
- **Matchers**: filter by tool name, file pattern, event reason
- **Async mode**: fire-and-forget background execution

For inbound webhooks (external → Claude): **Channels** accept push events
from Telegram, Discord, iMessage, or any custom MCP channel server.

**What claude-telegram needs to add:**
- Nothing. Claude Code hooks are strictly more powerful than OpenClaw's
  webhook system. Just configure them in `.claude/settings.json`.

**Why it's better:**
OpenClaw webhooks are a simple trigger mechanism. Claude Code hooks are a
full event-driven automation system with matchers, multiple execution modes,
scope inheritance (user → project → local), and admin control. The hook
can *block* an action (exit code 2), not just react to it.

---

## 9. ACP (Agent Client Protocol)

### What OpenClaw builds
A protocol for orchestrating external coding agents (Claude Code, Codex,
Gemini) via a unified interface. Pluggable backends via `acpx` runtime.
Handles routing, persistence, and multi-user on top.

### Claude Code equivalent: MCP + Agent Teams + claude-telegram itself

Claude Code's **MCP** is already the industry-standard protocol for tool
integration (adopted by OpenAI, Google, and others). It provides:
- Stdio, HTTP, and SSE transports
- OAuth authentication
- Dynamic tool discovery
- Channel push for real-time events

For multi-model orchestration: Claude Code Agent Teams can use different
models per teammate. MCP servers can wrap any external API (including
other AI providers) as callable tools.

claude-telegram itself is effectively an "ACP gateway" — it routes Telegram
messages to Claude Code sessions with full isolation, persistence, and
multi-user support.

**What claude-telegram needs to add:**
- To support non-Claude backends: write an MCP server that wraps the
  alternative provider's API. Claude calls it as a tool.
- This is a standard MCP pattern, not a new protocol.

**Why it's better:**
ACP is OpenClaw-specific and has no ecosystem. MCP is an open standard with
hundreds of existing servers, IDE support, and backing from every major AI
lab. Building on MCP means you get the entire ecosystem for free.

---

## Summary

| OpenClaw Feature | Claude Code Native | Extra Code Needed |
|---|---|---|
| Heartbeat | `/loop` | 0 lines (just a prompt) |
| Cron scheduler | `/loop` + `/schedule` | ~20 lines (hook for alerts) |
| Multi-agent | Agent Teams + Linux user isolation | ~30 lines (routing config) |
| Subagent orchestration | Agent Teams + worktrees | ~20 lines (status forwarding) |
| Agent-to-agent messaging | `claude-peers-mcp` | 0 lines (install MCP server) |
| System event queue | Hooks + channels | ~40 lines (event collector hook) |
| Channel health monitor | tmux + cron | ~15 lines (healthcheck.sh) |
| Hooks / webhooks | Native hooks (command/HTTP/prompt/agent) | 0 lines (config only) |
| ACP | MCP (open standard) | MCP server per provider |

**Total new code to match all OpenClaw features: ~125 lines of shell/Python + config.**

OpenClaw rebuilt the world from scratch. Claude Code already ships the primitives —
claude-telegram just needs thin wiring to expose them to Telegram users.
