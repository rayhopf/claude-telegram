#!/usr/bin/env python3
"""
router.py — Central orchestrator for the multi-tenant Claude-Telegram system.

Entry point. Runs as root (or with sudo access for useradd).
- Starts Telegram bot in background thread
- Manages user registry (Telegram ID -> Linux user -> Claude session)
- Creates Linux users on demand with isolation (umask 077, restricted group)
- Spawns claude_session.py per user in tmux
- Routes messages between Telegram and Claude sessions
- Admin TUI on the terminal

Usage: sudo python3 router.py [--config config.json]
"""

import json
import logging
import os
import select
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from protocol import (
    MSG_USER_MESSAGE, MSG_PERMISSION_RESPONSE, MSG_SHUTDOWN,
    MSG_ASSISTANT_TEXT, MSG_TOOL_CALL, MSG_TOOL_RESULT,
    MSG_PERMISSION_REQUEST, MSG_RESULT, MSG_ERROR, MSG_SESSION_READY,
    send_json, SocketReader, generate_linux_username,
)
from telegram_bot import TelegramBot

# Logging
_log_dir = os.path.expanduser("~/.claude/logs")
os.makedirs(_log_dir, exist_ok=True)
_log_file = os.path.join(_log_dir, f"router_{datetime.now():%Y%m%d_%H%M%S}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_log_file),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger("router")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    config_path = "config.json"
    if "--config" in sys.argv:
        config_path = sys.argv[sys.argv.index("--config") + 1]
    elif not os.path.exists(config_path):
        config_path = os.path.join(os.path.dirname(__file__), "config.json")

    with open(config_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# User record
# ---------------------------------------------------------------------------

@dataclass
class UserRecord:
    tg_user_id: int
    tg_first_name: str
    tg_username: str
    linux_username: str
    socket_path: str = ""
    socket_conn: Optional[socket.socket] = None
    reader_thread: Optional[threading.Thread] = None
    active: bool = False


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class Router:
    def __init__(self, config: dict):
        self.config = config
        self.users: dict[int, UserRecord] = {}  # tg_user_id -> UserRecord
        self.telegram_bot: Optional[TelegramBot] = None
        self.pending_permissions: dict[str, int] = {}  # request_id -> tg_user_id
        self.socket_dir = config.get("socket_dir", "/tmp")
        self.log_dir = os.path.expanduser(config.get("log_dir", "~/.claude/logs"))
        self.registry_path = os.path.join(self.log_dir, "user_registry.json")
        self.script_dir = config.get("install_dir", os.path.dirname(os.path.abspath(__file__)))

    def run(self):
        """Entry point."""
        self._print(f"Router starting...")
        self._print(f"Log: {_log_file}")
        self._print(f"Registry: {self.registry_path}")
        self._print(f"Scripts: {self.script_dir}")

        # Check that script dir is world-accessible (non-root users need to read it)
        script_dir_stat = os.stat(self.script_dir)
        if not (script_dir_stat.st_mode & 0o005):
            self._print(f"WARNING: {self.script_dir} is not world-readable!")
            self._print(f"  Per-user sessions won't be able to run claude_session.py")
            self._print(f"  Fix: install to /opt/claude-telegram or set install_dir in config")

        # Load existing user registry
        self._load_registry()

        # Reconnect to any live tmux sessions
        self._reconnect_sessions()

        # Start Telegram bot
        whitelist = set(self.config.get("whitelist_user_ids", []))
        self.telegram_bot = TelegramBot(
            token=self.config["telegram_bot_token"],
            whitelist_ids=whitelist,
            on_message=self._on_telegram_message,
            on_permission_response=self._on_telegram_permission,
        )
        self.telegram_bot.start()
        self._print("Telegram bot started")
        self._print("Admin TUI ready. Commands: @username message, /list, /kill username")
        self._print("-" * 60)

        # Admin TUI loop (main thread)
        try:
            self._admin_tui_loop()
        except KeyboardInterrupt:
            self._print("\nShutting down...")
        finally:
            self._shutdown()

    # ------------------------------------------------------------------
    # User registry persistence
    # ------------------------------------------------------------------

    def _load_registry(self):
        if not os.path.exists(self.registry_path):
            return
        try:
            with open(self.registry_path) as f:
                data = json.load(f)
            for uid_str, info in data.items():
                uid = int(uid_str)
                self.users[uid] = UserRecord(
                    tg_user_id=uid,
                    tg_first_name=info.get("tg_first_name", ""),
                    tg_username=info.get("tg_username", ""),
                    linux_username=info["linux_username"],
                )
            self._print(f"Loaded {len(self.users)} users from registry")
        except Exception as e:
            logger.warning("Failed to load registry: %s", e)

    def _save_registry(self):
        data = {}
        for uid, user in self.users.items():
            data[str(uid)] = {
                "linux_username": user.linux_username,
                "tg_first_name": user.tg_first_name,
                "tg_username": user.tg_username,
            }
        os.makedirs(os.path.dirname(self.registry_path), exist_ok=True)
        with open(self.registry_path, "w") as f:
            json.dump(data, f, indent=2)

    # ------------------------------------------------------------------
    # Reconnect to live tmux sessions on restart
    # ------------------------------------------------------------------

    def _reconnect_sessions(self):
        for uid, user in self.users.items():
            if self._tmux_session_alive(user.linux_username):
                self._print(f"Reconnecting to live session: {user.linux_username}")
                sock_path = os.path.join(self.socket_dir, f"claude-{user.linux_username}.sock")
                user.socket_path = sock_path
                if os.path.exists(sock_path):
                    try:
                        self._connect_to_session(user)
                        self._print(f"  Reconnected: {user.linux_username}")
                    except Exception as e:
                        self._print(f"  Failed to reconnect {user.linux_username}: {e}")
                        logger.warning("Reconnect failed for %s: %s", user.linux_username, e)

    def _tmux_session_alive(self, session_name: str) -> bool:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
        )
        return result.returncode == 0

    # ------------------------------------------------------------------
    # User management
    # ------------------------------------------------------------------

    def _ensure_user(self, tg_user_id: int, tg_first_name: str, tg_username: str) -> UserRecord:
        """Get or create a user record, Linux user, and Claude session."""
        user = self.users.get(tg_user_id)

        if user is None:
            # New user — create everything
            linux_username = generate_linux_username(tg_first_name, tg_user_id)
            user = UserRecord(
                tg_user_id=tg_user_id,
                tg_first_name=tg_first_name,
                tg_username=tg_username,
                linux_username=linux_username,
            )
            self.users[tg_user_id] = user
            self._save_registry()
            self._print(f"New user: {tg_first_name} (@{tg_username}) -> {linux_username}")

            # Create Linux user
            self._create_linux_user(linux_username)

        # Update names (they can change)
        user.tg_first_name = tg_first_name
        user.tg_username = tg_username

        # Ensure session is running
        if not user.active:
            self._spawn_session(user)
            self._connect_to_session(user)

        return user

    def _create_linux_user(self, username: str):
        """Create a restricted Linux user with isolated home directory."""
        group = self.config.get("restricted_group", "restricted")

        # Create group if needed
        subprocess.run(["groupadd", "-f", group], capture_output=True)

        # Check if user already exists
        result = subprocess.run(["id", username], capture_output=True)
        if result.returncode == 0:
            self._print(f"  Linux user {username} already exists")
            return

        # Create user
        subprocess.run([
            "useradd", "-m",
            "-G", group,
            "-s", "/bin/bash",
            username,
        ], check=True)

        # Set umask, env vars, and home dir permissions
        home = f"/home/{username}"
        # Write to .profile (not .bashrc) — .bashrc exits early for non-interactive shells
        profile = os.path.join(home, ".profile")
        with open(profile, "a") as f:
            f.write("\numask 077\n")
            # Pass through API env vars so Claude CLI works for this user
            for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"):
                val = os.environ.get(var)
                if val:
                    f.write(f"export {var}={val}\n")
        subprocess.run(["chmod", "700", home], check=True)

        self._print(f"  Created Linux user: {username} (home={home})")
        logger.info("Created Linux user: %s", username)

    def _spawn_session(self, user: UserRecord):
        """Spawn a claude_session.py process inside tmux for this user."""
        script = os.path.join(self.script_dir, "claude_session.py")
        sock_path = os.path.join(self.socket_dir, f"claude-{user.linux_username}.sock")
        user.socket_path = sock_path

        # Clean up stale socket
        if os.path.exists(sock_path):
            os.unlink(sock_path)

        # Kill existing tmux session if any
        if self._tmux_session_alive(user.linux_username):
            subprocess.run(["tmux", "kill-session", "-t", user.linux_username], capture_output=True)

        auto_approve = ",".join(self.config.get("auto_approve_tools", []))
        claude_cmd = self.config.get("claude_command", "claude")

        # Build the command that runs inside tmux
        # Pass through API env vars so Claude CLI works for the user
        env_exports = ""
        for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"):
            val = os.environ.get(var)
            if val:
                env_exports += f"export {var}={val}; "

        session_cmd = (
            f"{env_exports}"
            f"python3 {script}"
            f" --socket {sock_path}"
            f" --auto-approve '{auto_approve}'"
            f" --claude-command {claude_cmd}"
        )

        subprocess.run([
            "sudo", "-u", user.linux_username,
            "tmux", "new-session", "-d", "-s", user.linux_username,
            "bash", "-c", session_cmd,
        ], check=True)

        self._print(f"  Spawned tmux session: {user.linux_username}")
        logger.info("Spawned session for %s at %s", user.linux_username, sock_path)

        # Wait for socket to appear
        for _ in range(50):  # 5 seconds max
            if os.path.exists(sock_path):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"Socket {sock_path} did not appear within 5s")

    def _connect_to_session(self, user: UserRecord):
        """Connect to a user's claude_session via Unix socket."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(user.socket_path)
        user.socket_conn = sock
        user.active = True

        # Start reader thread
        user.reader_thread = threading.Thread(
            target=self._session_reader, args=(user,), daemon=True
        )
        user.reader_thread.start()

        # Wait for session_ready
        # (handled in reader thread, just give it a moment)
        time.sleep(0.5)

    # ------------------------------------------------------------------
    # Session reader (one thread per user)
    # ------------------------------------------------------------------

    def _session_reader(self, user: UserRecord):
        """Read messages from a claude_session and dispatch."""
        reader = SocketReader(user.socket_conn)
        try:
            for msg in reader:
                msg_type = msg.get("type", "")
                tg_user_id = user.tg_user_id

                if msg_type == MSG_SESSION_READY:
                    self._print(f"  [{user.linux_username}] Session ready")

                elif msg_type == MSG_ASSISTANT_TEXT:
                    text = msg.get("text", "")
                    preview = text[:100] + "..." if len(text) > 100 else text
                    self._print(f"  [{user.linux_username}] Claude: {preview}")
                    # Don't send assistant text directly — it comes with the result

                elif msg_type == MSG_TOOL_CALL:
                    tool = msg.get("tool_name", "?")
                    self._print(f"  [{user.linux_username}] Tool: {tool}")
                    if self.telegram_bot:
                        self.telegram_bot.update_stream(tg_user_id, f"Tool: {tool}")

                elif msg_type == MSG_TOOL_RESULT:
                    is_error = msg.get("is_error", False)
                    summary = msg.get("summary", "done")
                    if is_error:
                        self._print(f"  [{user.linux_username}] Error: {summary}")
                        if self.telegram_bot:
                            self.telegram_bot.update_stream(tg_user_id, f"Error: {summary}")
                    else:
                        if self.telegram_bot:
                            self.telegram_bot.update_stream(tg_user_id, f"  done")

                elif msg_type == MSG_PERMISSION_REQUEST:
                    request_id = msg.get("request_id", "")
                    tool_name = msg.get("tool_name", "?")
                    self.pending_permissions[request_id] = tg_user_id
                    self._print(f"  [{user.linux_username}] Permission: {tool_name} (id={request_id[:8]})")
                    if self.telegram_bot:
                        self.telegram_bot.send_permission_prompt(
                            tg_user_id, request_id, tool_name,
                            {"tool_input": msg.get("tool_input", {}), "reason": msg.get("reason", "")},
                        )

                elif msg_type == MSG_RESULT:
                    text = msg.get("text", "")
                    duration = msg.get("duration_ms")
                    cost = msg.get("cost_usd")
                    turns = msg.get("turns", 0)
                    info = []
                    if duration:
                        info.append(f"{duration/1000:.1f}s")
                    if turns:
                        info.append(f"{turns} turns")
                    if cost:
                        info.append(f"${cost:.4f}")
                    stats = " | ".join(info) if info else "Done"
                    self._print(f"  [{user.linux_username}] Result: {stats}")

                    if self.telegram_bot:
                        self.telegram_bot.finalize_stream(tg_user_id, stats)
                        if text:
                            stripped = text.strip()
                            is_json = (stripped.startswith("{") and stripped.endswith("}")) or \
                                      (stripped.startswith("[") and stripped.endswith("]"))
                            if not is_json:
                                self.telegram_bot.send_text(tg_user_id, text)

                elif msg_type == MSG_ERROR:
                    error = msg.get("error", "Unknown error")
                    self._print(f"  [{user.linux_username}] ERROR: {error}")
                    if self.telegram_bot:
                        self.telegram_bot.send_text(tg_user_id, f"Error: {error}")

        except Exception as e:
            logger.warning("Session reader for %s died: %s", user.linux_username, e)
            self._print(f"  [{user.linux_username}] Disconnected: {e}")
        finally:
            user.active = False

    # ------------------------------------------------------------------
    # Telegram callbacks
    # ------------------------------------------------------------------

    def _on_telegram_message(self, tg_user_id: int, tg_first_name: str,
                              tg_username: str, text: str, chat_id: int):
        """Called by TelegramBot when a user sends a message."""
        display = f"@{tg_username}" if tg_username else tg_first_name
        self._print(f"  [{display}] -> {text[:80]}")
        try:
            user = self._ensure_user(tg_user_id, tg_first_name, tg_username)
            send_json(user.socket_conn, {
                "type": MSG_USER_MESSAGE,
                "text": text,
            })
        except Exception as e:
            self._print(f"  ERROR routing message: {e}")
            logger.error("Failed to route message from %d: %s", tg_user_id, e)
            if self.telegram_bot:
                self.telegram_bot.send_text(tg_user_id, f"Error: {e}")

    def _on_telegram_permission(self, tg_user_id: int, request_id: str, allow: bool):
        """Called by TelegramBot when a user responds to a permission prompt."""
        owner_uid = self.pending_permissions.pop(request_id, None)
        if owner_uid is None:
            logger.warning("Permission response for unknown request: %s", request_id)
            return

        user = self.users.get(owner_uid)
        if not user or not user.socket_conn:
            return

        action = "Allowed" if allow else "Denied"
        self._print(f"  [{self._display_name(user)}] {action} (id={request_id[:8]})")

        send_json(user.socket_conn, {
            "type": MSG_PERMISSION_RESPONSE,
            "request_id": request_id,
            "allow": allow,
        })

    # ------------------------------------------------------------------
    # Admin TUI
    # ------------------------------------------------------------------

    def _admin_tui_loop(self):
        """Main thread: admin terminal interface."""
        while True:
            if select.select([sys.stdin], [], [], 0.5)[0]:
                try:
                    line = input().strip()
                except EOFError:
                    break
                if not line:
                    continue
                self._handle_admin_command(line)

    def _handle_admin_command(self, line: str):
        if line == "/list":
            self._print("Active sessions:")
            for uid, user in self.users.items():
                status = "ACTIVE" if user.active else "inactive"
                self._print(f"  {user.linux_username} | {self._display_name(user)} ({user.tg_first_name}) | {status}")
            if not self.users:
                self._print("  (none)")

        elif line.startswith("/kill "):
            target = line.split(None, 1)[1]
            user = self._find_user_by_name(target)
            if user:
                self._kill_session(user)
            else:
                self._print(f"  User not found: {target}")

        elif line.startswith("@"):
            parts = line.split(None, 1)
            if len(parts) < 2:
                self._print("  Usage: @username message")
                return
            target = parts[0][1:]  # strip @
            text = parts[1]
            user = self._find_user_by_name(target)
            if user and user.active and user.socket_conn:
                send_json(user.socket_conn, {
                    "type": MSG_USER_MESSAGE,
                    "text": text,
                })
                self._print(f"  -> [{user.linux_username}] {text[:80]}")
            elif user:
                self._print(f"  Session not active for {target}")
            else:
                self._print(f"  User not found: {target}")

        else:
            self._print("Commands: /list, /kill <username>, @<username> <message>")

    def _find_user_by_name(self, name: str) -> Optional[UserRecord]:
        """Find user by linux username or telegram username."""
        for user in self.users.values():
            if user.linux_username == name or user.tg_username == name:
                return user
        return None

    def _kill_session(self, user: UserRecord):
        """Kill a user's Claude session."""
        if user.socket_conn:
            try:
                send_json(user.socket_conn, {"type": MSG_SHUTDOWN})
            except Exception:
                pass
            user.socket_conn.close()
            user.socket_conn = None
        if self._tmux_session_alive(user.linux_username):
            subprocess.run(["tmux", "kill-session", "-t", user.linux_username], capture_output=True)
        user.active = False
        self._print(f"  Killed session: {user.linux_username}")

    def _shutdown(self):
        """Clean shutdown of all sessions."""
        for user in self.users.values():
            if user.active:
                self._kill_session(user)
        self._print("All sessions stopped.")

    def _display_name(self, user: UserRecord) -> str:
        """Return @username or first_name for display."""
        if user.tg_username:
            return f"@{user.tg_username}"
        return user.tg_first_name or user.linux_username

    def _print(self, text: str):
        """Print to admin terminal with timestamp."""
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {text}")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_config()
    router = Router(config)
    router.run()


if __name__ == "__main__":
    main()
