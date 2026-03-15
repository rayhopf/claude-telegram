# Deployment Guide

Step-by-step guide for deploying claude-telegram on a fresh Linux server.

## Prerequisites

- Linux server (Ubuntu/Debian recommended) with root access
- Node.js v22+ (for `npx skills`)
- Python 3.10+
- `claude` CLI installed and in PATH
- Telegram bot token from [@BotFather](https://t.me/BotFather)

## 1. Install the code

```bash
cd /opt
git clone https://github.com/rayhopf/claude-telegram.git
cd claude-telegram
chmod -R a+rX /opt/claude-telegram  # must be world-readable for per-user sessions
pip install python-telegram-bot
```

## 2. Configure

```bash
cp config.example.json config.json
```

Edit `config.json` with your bot token and settings. See README for all options.

## 3. Pre-install skills (optional)

Install skills once to a skeleton directory. These get copied to every new user automatically.

```bash
mkdir -p /opt/claude-telegram/skills-skel
cd /opt/claude-telegram/skills-skel
npx skills add --yes https://github.com/binance/binance-skills-hub
```

Then set in `config.json`:
```json
"skills_skel_dir": "/opt/claude-telegram/skills-skel/.claude/skills"
```

Note: `npx skills` creates symlinks in `.claude/skills/` pointing to `.agents/skills/`. The router resolves these automatically when copying to users.

## 4. Start the router

Using tmux so it survives SSH disconnection:

```bash
tmux new-session -d -s router \
  -e ANTHROPIC_BASE_URL=http://127.0.0.1:8317 \
  -e ANTHROPIC_API_KEY=claude-code-proxy-key \
  "cd /opt/claude-telegram && python3 router.py"
```

Adjust the env vars for your setup:
- Direct Anthropic API: set `ANTHROPIC_API_KEY` to your real key, omit `ANTHROPIC_BASE_URL`
- Codex proxy: set both as shown above (see [SPEC_codex_proxy_deployment.md](SPEC_codex_proxy_deployment.md))

Verify it started:
```bash
tmux capture-pane -t router -p
```

## 5. Update / redeploy

```bash
cd /opt/claude-telegram
git pull
tmux kill-session -t router
tmux new-session -d -s router \
  -e ANTHROPIC_BASE_URL=http://127.0.0.1:8317 \
  -e ANTHROPIC_API_KEY=claude-code-proxy-key \
  "cd /opt/claude-telegram && python3 router.py"
```

The router auto-reconnects to any live user tmux sessions on restart.

## Monitoring

### Router TUI

```bash
tmux attach -t router
```

Admin commands inside the TUI:
- `/list` — show all active sessions
- `@username message` — send a message to a user's Claude session
- `/kill username` — stop a user's session

### Watch a user session live

```bash
tmux attach -t <linux_username>
```

### List all tmux sessions

```bash
tmux list-sessions
```

### Logs

```bash
# Router logs
ls ~/.claude/logs/router_*.log

# Per-user session logs (inside each user's home)
ls /home/<username>/.claude/logs/
```

### User registry

```bash
cat ~/.claude/logs/user_registry.json
```

## User management

### Clean-wipe a user

Use this to fully reset a user (e.g., for testing). The user will be recreated on their next Telegram message.

```bash
USERNAME=<linux_username>
TG_USER_ID=<telegram_user_id>

# 1. Kill their tmux session and any processes
tmux kill-session -t $USERNAME 2>/dev/null
pkill -u $USERNAME 2>/dev/null
sleep 1

# 2. Delete the Linux user and home directory
userdel -f $USERNAME 2>/dev/null
rm -rf /home/$USERNAME

# 3. Clean up socket
rm -f /tmp/claude-$USERNAME.sock

# 4. Remove from registry
python3 -c "
import json
with open('$HOME/.claude/logs/user_registry.json') as f:
    data = json.load(f)
data.pop('$TG_USER_ID', None)
with open('$HOME/.claude/logs/user_registry.json', 'w') as f:
    json.dump(data, f, indent=2)
"
```

Then restart the router (step 5 above).

**Important:** You must `pkill -u` or `kill` all user processes before `userdel`, otherwise the delete silently fails.

### Find a user's linux username

```bash
python3 -c "
import json
with open('$HOME/.claude/logs/user_registry.json') as f:
    data = json.load(f)
for uid, info in data.items():
    print(f'{uid}: {info[\"linux_username\"]} ({info.get(\"tg_first_name\", \"\")} @{info.get(\"tg_username\", \"\")})')
"
```

## Troubleshooting

### Socket did not appear within 5s

The user's Claude session failed to start. Check:
```bash
# Does the user's home directory exist?
ls -la /home/<username>/

# Can the user run the session script?
sudo -u <username> python3 /opt/claude-telegram/claude_session.py --help

# Is /opt/claude-telegram world-readable?
ls -la /opt/claude-telegram/
```

### Permission denied on scripts

```bash
chmod -R a+rX /opt/claude-telegram
```

### User's Claude session exits immediately

Check the user's session log:
```bash
ls /home/<username>/.claude/logs/
cat /home/<username>/.claude/logs/session_*.log
```

Common causes:
- `claude` CLI not in PATH for the user
- API key not passed through (check `.profile` has the env exports)
- Proxy server not running
