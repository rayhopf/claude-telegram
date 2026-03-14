#!/usr/bin/env python3
"""
claude_session.py — Per-user Claude Code subprocess manager.

Runs as a dedicated Linux user inside a tmux session.
Communicates with the router via a Unix domain socket.
Manages one Claude CLI subprocess using the stream-json protocol.

Usage:
    python3 claude_session.py --socket /tmp/claude-USERNAME.sock \
        --auto-approve Read,Grep,Glob,Bash \
        --claude-command claude
"""

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import threading
from datetime import datetime

from protocol import (
    MSG_ASSISTANT_TEXT, MSG_TOOL_CALL, MSG_TOOL_RESULT,
    MSG_PERMISSION_REQUEST, MSG_RESULT, MSG_ERROR, MSG_SESSION_READY,
    MSG_USER_MESSAGE, MSG_PERMISSION_RESPONSE, MSG_SHUTDOWN, MSG_RESTART,
    send_json, SocketReader,
)

# Logging to file (inside user's home)
_log_dir = os.path.expanduser("~/.claude/logs")
os.makedirs(_log_dir, exist_ok=True)
_log_file = os.path.join(_log_dir, f"session_{datetime.now():%Y%m%d_%H%M%S}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    filename=_log_file,
    filemode="a",
)
logger = logging.getLogger("claude-session")


class ClaudeSession:
    def __init__(self, socket_path: str, auto_approve_tools: set,
                 claude_command: str = "claude"):
        self.socket_path = socket_path
        self.auto_approve_tools = auto_approve_tools
        self.claude_command = claude_command
        self.proc = None
        self.conn = None  # connection from router
        self.stdin_lock = threading.Lock()
        self.pending_permissions = {}  # request_id -> threading.Event
        self.permission_responses = {}  # request_id -> response dict
        self.session_id = None  # tracked from Claude's system message
        self._restarting = False  # suppress exit error during restart

    def run(self):
        """Main entry: listen on socket, spawn Claude, bridge messages."""
        print(f"[Session] Listening on {self.socket_path}")
        logger.info("Starting session, socket=%s", self.socket_path)

        self._setup_socket()
        print("[Session] Router connected")
        logger.info("Router connected")

        # Tell router we're ready
        send_json(self.conn, {"type": MSG_SESSION_READY})

        self._spawn_claude()
        print(f"[Session] Claude started (pid={self.proc.pid})")
        logger.info("Claude process started (pid=%d)", self.proc.pid)

        # Thread: read Claude stdout -> parse -> send to router
        claude_reader = threading.Thread(
            target=self._read_claude_output, daemon=True
        )
        claude_reader.start()

        # Main thread: read router messages -> forward to Claude
        try:
            self._read_router_messages()
        except KeyboardInterrupt:
            print("\n[Session] Interrupted")
        finally:
            self._cleanup()

    def _setup_socket(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.socket_path)
        # Allow router (root) to connect
        os.chmod(self.socket_path, 0o770)
        self.server.listen(1)
        self.conn, _ = self.server.accept()

    def _spawn_claude(self, resume_session_id: str = None):
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        cmd = [
            self.claude_command,
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--permission-prompt-tool", "stdio",
        ]
        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        )

    def _send_to_claude(self, msg: dict):
        with self.stdin_lock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def _send_to_router(self, msg: dict):
        try:
            send_json(self.conn, msg)
        except (BrokenPipeError, OSError) as e:
            logger.warning("Failed to send to router: %s", e)

    # ------------------------------------------------------------------
    # Read Claude stdout (runs in thread)
    # ------------------------------------------------------------------

    def _read_claude_output(self):
        for raw_line in self.proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                print(f"[raw] {line}")
                continue

            msg_type = msg.get("type", "")

            if msg_type == "assistant":
                message = msg.get("message", {})
                for block in message.get("content", []):
                    if block.get("type") == "text":
                        text = block["text"]
                        print(f"\n\033[36mClaude:\033[0m {text}")
                        self._send_to_router({
                            "type": MSG_ASSISTANT_TEXT,
                            "text": text,
                        })
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        inp = json.dumps(block.get("input", {}))
                        if len(inp) > 120:
                            inp = inp[:120] + "..."
                        print(f"\n\033[33mTool:\033[0m {name}({inp})")
                        self._send_to_router({
                            "type": MSG_TOOL_CALL,
                            "tool_name": name,
                            "summary": f"{name}",
                        })

            elif msg_type == "user":
                message = msg.get("message", {})
                for block in message.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        content = block.get("content", "")
                        is_error = block.get("is_error", False)
                        preview = content[:200] if isinstance(content, str) else json.dumps(content)[:200]
                        if is_error:
                            print(f"   \033[31mError:\033[0m {preview}")
                            self._send_to_router({
                                "type": MSG_TOOL_RESULT,
                                "summary": preview[:80],
                                "is_error": True,
                            })
                        else:
                            print(f"   \033[90mResult:\033[0m {preview}")
                            self._send_to_router({
                                "type": MSG_TOOL_RESULT,
                                "summary": "done",
                                "is_error": False,
                            })

            elif msg_type == "result":
                result_text = msg.get("result", "")
                duration = msg.get("duration_ms")
                cost = msg.get("total_cost_usd")
                turns = msg.get("num_turns", 0)
                info = []
                if duration:
                    info.append(f"{duration/1000:.1f}s")
                if turns:
                    info.append(f"{turns} turns")
                if cost:
                    info.append(f"${cost:.4f}")
                if info:
                    print(f"   \033[90m{' | '.join(info)}\033[0m")

                self._send_to_router({
                    "type": MSG_RESULT,
                    "text": result_text,
                    "duration_ms": duration,
                    "cost_usd": cost,
                    "turns": turns,
                })

            elif msg_type == "system":
                session_id = msg.get("session_id", "")
                model = msg.get("model", "")
                if session_id:
                    self.session_id = session_id
                    print(f"   \033[90mSession: {session_id} | Model: {model}\033[0m")

            elif msg_type == "control_request":
                self._handle_permission(msg)

            elif msg_type == "error":
                error = msg.get("error", str(msg))
                print(f"\n\033[31mError:\033[0m {error}")
                self._send_to_router({
                    "type": MSG_ERROR,
                    "error": error,
                })

        if self._restarting:
            print("\n[Session restarting...]")
        else:
            print("\n[Session ended]")
            self._send_to_router({"type": MSG_ERROR, "error": "Claude process exited"})

    # ------------------------------------------------------------------
    # Permission handling
    # ------------------------------------------------------------------

    def _handle_permission(self, msg):
        request = msg.get("request", {})
        request_id = msg.get("request_id", "")
        tool_name = request.get("tool_name", "?")
        tool_input = request.get("input", {})

        # Auto-approve safe tools
        if tool_name in self.auto_approve_tools:
            print(f"\n\033[32mAuto-approved:\033[0m {tool_name}")
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": {
                        "behavior": "allow",
                        "updatedInput": tool_input,
                    },
                },
            }
            self._send_to_claude(response)
            return

        # Forward to router for Telegram/admin approval
        reason = request.get("decision_reason", "")
        print(f"\n\033[1;33mPermission requested:\033[0m {tool_name}")
        for key in ["file_path", "command", "pattern", "url", "description"]:
            if key in tool_input:
                val = str(tool_input[key])
                if len(val) > 200:
                    val = val[:200] + "..."
                print(f"   {key}: {val}")

        self._send_to_router({
            "type": MSG_PERMISSION_REQUEST,
            "request_id": request_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "reason": reason,
        })

        # Wait for response from router
        event = threading.Event()
        self.pending_permissions[request_id] = event
        print("   Waiting for approval...")

        event.wait()  # blocks until router responds

        resp = self.permission_responses.pop(request_id, None)
        del self.pending_permissions[request_id]

        if resp and resp.get("allow"):
            print(f"   \033[32mAllowed\033[0m")
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": {
                        "behavior": "allow",
                        "updatedInput": tool_input,
                    },
                },
            }
        else:
            message = (resp or {}).get("message", "Denied")
            print(f"   \033[31mDenied:\033[0m {message}")
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": {
                        "behavior": "deny",
                        "message": message,
                    },
                },
            }

        self._send_to_claude(response)

    # ------------------------------------------------------------------
    # Read router messages (runs on main thread)
    # ------------------------------------------------------------------

    def _read_router_messages(self):
        reader = SocketReader(self.conn)
        for msg in reader:
            msg_type = msg.get("type", "")

            if msg_type == MSG_USER_MESSAGE:
                text = msg["text"]
                print(f"\n\033[35mUser:\033[0m {text}")
                claude_msg = {
                    "type": "user",
                    "session_id": "",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": text}],
                    },
                    "parent_tool_use_id": None,
                }
                self._send_to_claude(claude_msg)

            elif msg_type == MSG_PERMISSION_RESPONSE:
                request_id = msg.get("request_id", "")
                event = self.pending_permissions.get(request_id)
                if event:
                    self.permission_responses[request_id] = msg
                    event.set()
                else:
                    logger.warning("Permission response for unknown request: %s", request_id)

            elif msg_type == MSG_RESTART:
                print("[Session] Restart requested")
                self._restart_claude()

            elif msg_type == MSG_SHUTDOWN:
                print("[Session] Shutdown requested")
                break

    def _restart_claude(self):
        """Kill current Claude process and respawn with --resume."""
        resume_id = self.session_id
        print(f"[Session] Restarting Claude (resume={resume_id})")
        logger.info("Restarting Claude, resume=%s", resume_id)

        # Suppress exit error from old reader thread
        self._restarting = True

        # Kill current process
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

        # Respawn with resume
        self._restarting = False
        self._spawn_claude(resume_session_id=resume_id)
        print(f"[Session] Claude restarted (pid={self.proc.pid})")
        logger.info("Claude restarted (pid=%d)", self.proc.pid)

        # New reader thread for the new process
        claude_reader = threading.Thread(
            target=self._read_claude_output, daemon=True
        )
        claude_reader.start()

        # Router handles the user-facing notification

    def _cleanup(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.conn:
            self.conn.close()
        if self.server:
            self.server.close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        logger.info("Session cleaned up")


def main():
    parser = argparse.ArgumentParser(description="Claude session manager")
    parser.add_argument("--socket", required=True, help="Unix socket path")
    parser.add_argument("--auto-approve", default="", help="Comma-separated tool names to auto-approve")
    parser.add_argument("--claude-command", default="claude", help="Path to claude CLI")
    args = parser.parse_args()

    auto_approve = set(t.strip() for t in args.auto_approve.split(",") if t.strip())

    session = ClaudeSession(
        socket_path=args.socket,
        auto_approve_tools=auto_approve,
        claude_command=args.claude_command,
    )
    session.run()


if __name__ == "__main__":
    main()
