# claude-telegram

Talk to [Claude Code](https://claude.ai/code) via Telegram.

A single-file bridge that spawns Claude Code CLI as a subprocess using its `stream-json` protocol, and connects it to a Telegram bot. You chat on Telegram, Claude Code does the work on your machine.

## How it works

```
Telegram ←→ claude-telegram.py ←→ claude CLI (stream-json stdin/stdout) ←→ Anthropic API
```

- User messages from Telegram are sent to Claude Code as JSON via stdin
- Claude Code responses are read from stdout as structured JSON and forwarded to Telegram
- Permission prompts (file edits, shell commands, etc.) appear as inline buttons (✅ Allow / ❌ Deny)
- A live "Working..." status message updates as Claude uses tools
- Everything is logged to a JSONL file

## Setup

1. **Create a Telegram bot** via [@BotFather](https://t.me/BotFather) and get the token.

2. **Create config.json** from the example:
   ```bash
   cp config.example.json config.json
   ```
   Edit `config.json` with your bot token and Telegram username:
   ```json
   {
     "telegram_bot_token": "123456:ABC-DEF...",
     "whitelist_usernames": ["your_username"],
     "log_dir": "~/.claude/logs"
   }
   ```

3. **Install dependency**:
   ```bash
   pip install python-telegram-bot==21.5
   ```

4. **Run**:
   ```bash
   python claude-telegram.py
   ```

Requires `claude` CLI to be installed and authenticated.

## Features

- **Permission handling** — When Claude needs to write files or run commands outside its sandbox, you get inline keyboard buttons on Telegram to approve or deny.
- **Live status** — The bot edits a "Working..." message as Claude processes your request, showing which tools are being used.
- **Full logging** — All messages (user input, Claude responses, tool calls, permission decisions) are saved as JSONL to `~/.claude/logs/`.
- **Whitelist** — Only specified Telegram usernames can use the bot.

## Architecture difference from codex-bridge

| | codex-bridge (Codex) | claude-telegram (Claude Code) |
|---|---|---|
| IPC | tmux send-keys + pipe-pane | JSON stdin/stdout |
| Parsing | Raw bytes + ANSI stripping | Structured JSON |
| Polling | File system polling (1s) | Async readline |
| Permissions | Not supported | Inline keyboard buttons |
| Files | 5 files + SQLite | 1 file + config |
