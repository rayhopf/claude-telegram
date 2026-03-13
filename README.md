# claude-telegram

A multi-tenant Claude Code platform with Telegram interface and per-user Linux isolation.

Each Telegram user gets their own Claude Code session running as a dedicated Linux user inside tmux — fully isolated, monitorable, and controllable from both Telegram and a terminal admin TUI.

## Architecture

```
Telegram users ──→ telegram_bot.py (thread)
                         ↕
                     router.py ←── Admin TUI (terminal)
                    ↙    ↓    ↘
           claude_session.py  (one per user, in tmux, as Linux user)
                    ↕
              claude CLI (stream-json)
```

### Components

| File | Role |
|------|------|
| `router.py` | Entry point. Orchestrates everything: user registry, Linux user creation, session lifecycle, message routing, admin TUI. |
| `telegram_bot.py` | Telegram frontend. Handles multi-user message I/O, permission buttons, streaming progress. Runs as a thread within router. |
| `claude_session.py` | Per-user Claude subprocess manager. Runs inside tmux as a dedicated Linux user. Communicates with router via Unix domain socket. |
| `protocol.py` | Shared utilities: socket JSON-lines helpers, username generator, message type constants. |
| `claude-telegram.py` | Legacy single-user mode (original monolithic version, still works standalone). |

## How it works

1. A Telegram user sends a message to the bot
2. Router checks authorization (empty whitelist = anyone allowed)
3. For new users: creates a Linux user (`useradd -m -G restricted`), sets umask 077, spawns a Claude session in tmux
4. Message is forwarded via Unix socket to that user's Claude session
5. Claude responses flow back through the socket to the router, then to Telegram
6. Admin sees all activity in the terminal and can interact with any session

### User isolation

Each user gets:
- A dedicated Linux user named `{first_char}{md5(tg_id)[:6]}{last_char}` (e.g., Tom -> `t827ccbm`)
- A private home directory (`chmod 700`, `umask 077`)
- Membership in a `restricted` group
- Their own Claude CLI process running inside a tmux session

Users can run system tools normally but cannot access each other's home directories.

### Monitoring

- **Admin TUI**: The terminal where `router.py` runs shows all user activity with timestamps
- **tmux attach**: Run `tmux attach -t {username}` to watch any user's Claude session live
- **Admin commands**: Type `@username message` to send to a user's session, `/list` to see active sessions, `/kill username` to stop a session

## Setup

1. **Create a Telegram bot** via [@BotFather](https://t.me/BotFather) and get the token.

2. **Create config.json** from the example:
   ```bash
   cp config.example.json config.json
   ```
   Edit with your bot token:
   ```json
   {
     "telegram_bot_token": "123456:ABC-DEF...",
     "whitelist_user_ids": [],
     "log_dir": "~/.claude/logs",
     "auto_approve_tools": ["WebSearch", "WebFetch", "Read", "Grep", "Glob", "Bash", "Agent", "ToolSearch"],
     "socket_dir": "/tmp",
     "restricted_group": "restricted",
     "claude_command": "claude"
   }
   ```
   Set `whitelist_user_ids` to an empty array `[]` to allow anyone, or add specific Telegram user IDs like `[12345, 67890]`.

3. **Install dependency**:
   ```bash
   pip install python-telegram-bot
   ```

4. **Run** (requires root for user creation):
   ```bash
   sudo python3 router.py
   ```
   Or with a custom config:
   ```bash
   sudo python3 router.py --config /path/to/config.json
   ```

Requires `claude` CLI to be installed and available in PATH.

## Config options

| Key | Required | Description |
|-----|----------|-------------|
| `telegram_bot_token` | yes | Bot token from @BotFather |
| `whitelist_user_ids` | no | Array of Telegram user IDs. Empty = allow anyone. |
| `log_dir` | no | Directory for logs and user registry (default: `~/.claude/logs`) |
| `auto_approve_tools` | no | Tools to auto-approve without prompting |
| `socket_dir` | no | Directory for Unix sockets (default: `/tmp`) |
| `restricted_group` | no | Linux group for user isolation (default: `restricted`) |
| `claude_command` | no | Path to claude CLI (default: `claude`) |

## Legacy single-user mode

The original `claude-telegram.py` still works as a standalone single-user bridge:
```bash
python3 claude-telegram.py
```
This uses the old config format with `whitelist_usernames` (string array) and runs everything in one process.

## Using with OpenAI Codex backend

See [SPEC_codex_proxy_deployment.md](SPEC_codex_proxy_deployment.md) for a reference on how to run Claude Code through a CLIProxyAPI proxy backed by an OpenAI Codex subscription. This is only relevant if you don't have enough Claude/Anthropic API tokens and want to use your Codex subscription as a fallback.
