"""
protocol.py — Shared utilities for the multi-tenant Claude-Telegram system.

Provides:
- Unix socket JSON-lines helpers (send/receive)
- Linux username generation from Telegram identity
- Message type constants
"""

import hashlib
import json
import socket


# ---------------------------------------------------------------------------
# Message types: Router -> Session
# ---------------------------------------------------------------------------
MSG_USER_MESSAGE = "user_message"
MSG_PERMISSION_RESPONSE = "permission_response"
MSG_SHUTDOWN = "shutdown"

# ---------------------------------------------------------------------------
# Message types: Session -> Router
# ---------------------------------------------------------------------------
MSG_ASSISTANT_TEXT = "assistant_text"
MSG_TOOL_CALL = "tool_call"
MSG_TOOL_RESULT = "tool_result"
MSG_PERMISSION_REQUEST = "permission_request"
MSG_RESULT = "result"
MSG_ERROR = "error"
MSG_SESSION_READY = "session_ready"


# ---------------------------------------------------------------------------
# Socket helpers (JSON-lines over Unix domain sockets)
# ---------------------------------------------------------------------------

def send_json(sock: socket.socket, obj: dict) -> None:
    """Write a JSON object as a single line to the socket."""
    data = json.dumps(obj, default=str) + "\n"
    sock.sendall(data.encode("utf-8"))


class SocketReader:
    """Buffered reader that yields complete JSON lines from a socket."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = b""

    def __iter__(self):
        return self

    def __next__(self) -> dict:
        while True:
            # Check if we already have a complete line in the buffer
            nl = self.buf.find(b"\n")
            if nl != -1:
                line = self.buf[:nl]
                self.buf = self.buf[nl + 1:]
                if line:
                    return json.loads(line)
                continue

            # Read more data
            chunk = self.sock.recv(4096)
            if not chunk:
                # Connection closed
                if self.buf:
                    remaining = self.buf
                    self.buf = b""
                    return json.loads(remaining)
                raise StopIteration
            self.buf += chunk


# ---------------------------------------------------------------------------
# Username generation
# ---------------------------------------------------------------------------

def generate_linux_username(tg_first_name: str, tg_user_id: int) -> str:
    """Generate a Linux username from Telegram identity.

    Format: first_char + md5(tg_user_id)[:6] + last_char
    Example: "Tom" with id=12345 -> "t827ccb" + "m" -> "t827ccbm"

    Handles edge cases:
    - Empty/None first_name: use "u" as both chars
    - Single char name: same char for first and last
    - Non-ASCII chars: filter to [a-z], fall back to "u"
    """
    name = (tg_first_name or "u").lower()
    safe = "".join(c for c in name if c.isascii() and c.isalpha())
    if not safe:
        safe = "u"

    first = safe[0]
    last = safe[-1]
    hash_part = hashlib.md5(str(tg_user_id).encode()).hexdigest()[:6]
    return f"{first}{hash_part}{last}"
