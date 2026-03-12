# claude-telegram

A hybrid CLI + Telegram bridge for [Claude Code](https://claude.ai/code).

Runs Claude Code as a normal terminal TUI (tool calls, results, permissions all visible) while also bridging messages to/from Telegram — so you can interact from your phone or desktop without being at the terminal.

## How it works

```
Terminal (stdin/stdout)
        ↕
claude-telegram.py ←→ claude CLI (stream-json) ←→ Anthropic API
        ↕
Telegram bot (background thread)
```

- **Terminal**: Full Claude Code output — tool calls, results, permissions, stats — like running `claude` directly
- **Telegram**: Send messages, see live streaming progress (tool calls updating via `edit_message`), receive final answers, approve/deny permissions with inline buttons
- Either channel can send messages or answer permission prompts — whichever responds first wins

## Telegram UX

Each request produces two messages:

1. **Streaming message** — starts as "Thinking...", edits in real-time as tools run (`🔧 Read`, `✅ Auto-approved: Bash`, etc.), ends with stats (`✅ 12.3s | 5 turns | $0.08`)
2. **Final answer** — Claude's response as a separate message (JSON results are skipped)

Permission prompts appear with **✅ Allow / ❌ Deny** inline buttons.

## Setup

1. **Create a Telegram bot** via [@BotFather](https://t.me/BotFather) and get the token.

2. **Create config.json** from the example:
   ```bash
   cp config.example.json config.json
   ```
   Edit with your bot token and Telegram username:
   ```json
   {
     "telegram_bot_token": "123456:ABC-DEF...",
     "whitelist_usernames": ["your_username"],
     "log_dir": "~/.claude/logs",
     "auto_approve_tools": ["WebSearch", "WebFetch", "Read", "Grep", "Glob", "Bash", "Agent", "ToolSearch"]
   }
   ```

3. **Install dependency**:
   ```bash
   pip install python-telegram-bot
   ```

4. **Run**:
   ```bash
   python claude-telegram.py
   ```
   Or with a custom config path:
   ```bash
   python claude-telegram.py --config /path/to/config.json
   ```

Requires `claude` CLI to be installed and authenticated.

## Features

- **Dual I/O** — Use terminal and Telegram simultaneously. Messages from either channel are sent to the same Claude session.
- **Auto-approve safe tools** — Configurable list of tools that skip permission prompts (default: read-only tools + Bash).
- **Permission from anywhere** — Permission prompts show in both terminal and Telegram. First response wins.
- **Live streaming** — Telegram status message updates on every tool call, no buffering.
- **Full logging** — All messages (user input, Claude responses, tool calls, permissions) saved as JSONL to `~/.claude/logs/`.
- **Clean terminal** — Library logs (httpx, telegram SDK) go to a debug log file, not the TUI.
- **Whitelist** — Only specified Telegram usernames can use the bot.

## Config options

| Key | Required | Description |
|---|---|---|
| `telegram_bot_token` | yes | Bot token from @BotFather |
| `whitelist_usernames` | yes | Array of allowed Telegram usernames |
| `log_dir` | no | Directory for JSONL logs (default: `~/.claude/logs`) |
| `auto_approve_tools` | no | Tools to auto-approve without prompting |

## Using with OpenAI Codex backend

See [SPEC_codex_proxy_deployment.md](SPEC_codex_proxy_deployment.md) for a reference on how to run Claude Code through a CLIProxyAPI proxy backed by an OpenAI Codex subscription. This is only relevant if you don't have enough Claude/Anthropic API tokens and want to use your Codex subscription as a fallback.
