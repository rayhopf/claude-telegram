#!/usr/bin/env python3
"""
claude-telegram.py — Claude CLI wrapper with Telegram bridge.

Like claude-logged.py, shows full Claude output in the terminal (tool calls,
results, permissions). Additionally bridges messages to/from Telegram so you
can interact remotely.

Usage: python3 claude-telegram.py [--config config.json]
"""

import asyncio
import json
import logging
import os
import select
import signal
import sys
import threading
import queue
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# Send all logs to file, keep terminal clean
_log_file = os.path.join(
    os.path.expanduser("~/.claude/logs"),
    f"telegram_debug_{datetime.now():%Y%m%d_%H%M%S}.log",
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    filename=_log_file,
    filemode="a",
)
# Silence noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger("claude-telegram")

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


CONFIG = load_config()
TOKEN = CONFIG["telegram_bot_token"]
WHITELIST = set(CONFIG.get("whitelist_usernames", []))
LOG_DIR = os.path.expanduser(CONFIG.get("log_dir", "~/.claude/logs"))
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, f"telegram_{datetime.now():%Y%m%d_%H%M%S}.jsonl")

# Tools that are auto-approved (read-only / safe)
AUTO_APPROVE_TOOLS = set(CONFIG.get("auto_approve_tools", [
    "WebSearch", "WebFetch",
    "Read", "Grep", "Glob",
    "Bash",  # remove if you want to review shell commands
    "Agent",
    "ToolSearch",
]))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_msg(direction, data):
    entry = {
        "ts": datetime.now().isoformat(),
        "direction": direction,
        "data": data,
    }
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")

# ---------------------------------------------------------------------------
# Claude process (synchronous subprocess, like claude-logged.py)
# ---------------------------------------------------------------------------

# Queues for cross-thread communication
permission_queue = queue.Queue()       # permission requests -> main thread
telegram_msg_queue = queue.Queue()     # telegram messages -> main thread
stdin_lock = threading.Lock()

# Shared state for Telegram status updates
telegram_state = {
    "bot": None,
    "chat_id": None,
    "status_msg_id": None,    # streaming/progress message
    "stream_lines": [],       # lines for streaming msg (tool calls, auto-approvals, etc.)
    "loop": None,             # asyncio event loop for telegram
}


def send_to_claude(proc, msg):
    """Thread-safe write to claude's stdin."""
    with stdin_lock:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()


def _escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tg_send(text, parse_mode=None, reply_markup=None):
    """Send a Telegram message from any thread (non-blocking)."""
    bot = telegram_state["bot"]
    chat_id = telegram_state["chat_id"]
    loop = telegram_state["loop"]
    if not bot or not chat_id or not loop:
        return

    async def _send():
        try:
            kwargs = {"chat_id": chat_id, "text": text}
            if parse_mode:
                kwargs["parse_mode"] = parse_mode
            if reply_markup:
                kwargs["reply_markup"] = reply_markup
            return await bot.send_message(**kwargs)
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)

    future = asyncio.run_coroutine_threadsafe(_send(), loop)
    try:
        return future.result(timeout=10)
    except Exception:
        pass


def _tg_edit_status(text):
    """Edit the Telegram status message from any thread."""
    bot = telegram_state["bot"]
    chat_id = telegram_state["chat_id"]
    msg_id = telegram_state["status_msg_id"]
    loop = telegram_state["loop"]
    if not bot or not chat_id or not msg_id or not loop:
        return

    async def _edit():
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
            )
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.warning("Telegram edit failed: %s", e)

    future = asyncio.run_coroutine_threadsafe(_edit(), loop)
    try:
        future.result(timeout=10)
    except Exception:
        pass


def _tg_stream(line):
    """Append a line to the streaming message and edit it immediately."""
    telegram_state["stream_lines"].append(line)
    lines = telegram_state["stream_lines"]

    text = "🤖 Working...\n\n" + "\n".join(lines)
    # Telegram message limit is 4096
    if len(text) > 4000:
        text = text[:500] + "\n...\n" + text[-3400:]

    _tg_edit_status(text)


def read_output(proc):
    """Read claude's stdout, print to terminal, forward to Telegram."""
    for raw_line in proc.stdout:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(f"[raw] {line}")
            continue

        log_msg("recv", msg)
        msg_type = msg.get("type", "")

        if msg_type == "assistant":
            message = msg.get("message", {})
            for block in message.get("content", []):
                if block.get("type") == "text":
                    text = block["text"]
                    print(f"\n\033[36m🤖 Claude:\033[0m {text}")
                    # Don't put Claude's text in streaming msg — it goes in final result
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    inp = json.dumps(block.get("input", {}))
                    if len(inp) > 120:
                        inp = inp[:120] + "..."
                    print(f"\n\033[33m🔧 Tool:\033[0m {name}({inp})")
                    _tg_stream(f"🔧 {name}")

        elif msg_type == "user":
            message = msg.get("message", {})
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block.get("content", "")
                    is_error = block.get("is_error", False)
                    preview = content[:200] if isinstance(content, str) else json.dumps(content)[:200]
                    if is_error:
                        print(f"   \033[31m↳ Error:\033[0m {preview}")
                        _tg_stream(f"❌ {preview[:80]}")
                    else:
                        print(f"   \033[90m↳ Result:\033[0m {preview}")
                        _tg_stream(f"   ↳ done")

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
                print(f"   \033[90m⏱  {' | '.join(info)}\033[0m")

            # Finalize streaming message — keep history, append stats
            status_line = "✅ " + " | ".join(info) if info else "✅ Done"
            lines = telegram_state["stream_lines"]
            if lines:
                final_text = "\n".join(lines) + "\n\n" + status_line
            else:
                final_text = status_line
            if len(final_text) > 4000:
                final_text = final_text[:500] + "\n...\n" + final_text[-3400:]
            _tg_edit_status(final_text)

            # Send final answer as a separate message (skip if looks like JSON)
            if result_text:
                stripped = result_text.strip()
                is_json = (stripped.startswith("{") and stripped.endswith("}")) or \
                          (stripped.startswith("[") and stripped.endswith("]"))
                if not is_json:
                    for i in range(0, len(result_text), 4000):
                        _tg_send(result_text[i:i + 4000])

            # Reset streaming state for next message
            telegram_state["status_msg_id"] = None
            telegram_state["stream_lines"] = []

        elif msg_type == "system":
            session_id = msg.get("session_id", "")
            model = msg.get("model", "")
            if session_id and model:
                print(f"   \033[90mSession: {session_id} | Model: {model}\033[0m")
            elif session_id:
                print(f"   \033[90mSession: {session_id}\033[0m")

        elif msg_type == "control_request":
            # Permission prompt — put in queue for main thread
            permission_queue.put(msg)

        elif msg_type == "error":
            error = msg.get("error", str(msg))
            print(f"\n\033[31m❌ Error:\033[0m {error}")
            _tg_send(f"❌ Error: {error}")

        elif msg_type == "rate_limit_event":
            pass

    print("\n[Session ended]")


def handle_permission(proc, msg):
    """Handle a permission request — auto-approve safe tools, prompt for others."""
    request = msg.get("request", {})
    request_id = msg.get("request_id", "")
    tool_name = request.get("tool_name", "?")
    tool_input = request.get("input", {})
    suggestions = request.get("permission_suggestions", [])
    reason = request.get("decision_reason", "")

    # Auto-approve safe tools
    if tool_name in AUTO_APPROVE_TOOLS:
        print(f"\n\033[32m✅ Auto-approved:\033[0m {tool_name}")
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
        log_msg("approval", {"action": "auto-allow", "tool": tool_name, "input": tool_input})
        log_msg("send", response)
        send_to_claude(proc, response)
        _tg_stream(f"✅ Auto-approved: {tool_name}")
        return

    print(f"\n\033[1;33m⚠️  Permission requested:\033[0m")
    print(f"   Tool: \033[1m{tool_name}\033[0m")
    if reason:
        print(f"   Reason: {reason}")

    for key in ["file_path", "command", "pattern", "url", "description"]:
        if key in tool_input:
            val = str(tool_input[key])
            if len(val) > 200:
                val = val[:200] + "..."
            print(f"   {key}: {val}")

    inp_str = json.dumps(tool_input)
    if len(inp_str) <= 300 and not any(k in tool_input for k in ["file_path", "command"]):
        print(f"   Input: {inp_str}")

    if suggestions:
        print(f"   Suggestions: {suggestions}")

    # Also send to Telegram with buttons
    tg_lines = [f"⚠️ <b>Permission requested</b>"]
    tg_lines.append(f"Tool: <code>{_escape(tool_name)}</code>")
    if reason:
        tg_lines.append(f"Reason: {_escape(reason)}")
    for key in ["file_path", "command", "url", "pattern", "description"]:
        if key in tool_input:
            val = str(tool_input[key])
            if len(val) > 200:
                val = val[:200] + "..."
            tg_lines.append(f"{key}: <code>{_escape(val)}</code>")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Allow", callback_data=f"perm:allow:{request_id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"perm:deny:{request_id}"),
        ]
    ])
    _tg_send("\n".join(tg_lines), parse_mode=ParseMode.HTML, reply_markup=keyboard)

    # Wait for answer from terminal OR Telegram
    print()
    answer = None
    while answer is None:
        # Check Telegram permission responses
        try:
            tg_answer = telegram_msg_queue.get(timeout=0.1)
            if tg_answer.get("type") == "permission_response" and tg_answer.get("request_id") == request_id:
                answer = "y" if tg_answer["allow"] else "n"
                print(f"   \033[90m(Answered via Telegram: {'Allow' if tg_answer['allow'] else 'Deny'})\033[0m")
                continue
            # Not a matching permission response, put it back... or handle as message
        except queue.Empty:
            pass

        # Check terminal input
        if select.select([sys.stdin], [], [], 0.1)[0]:
            try:
                answer = input("   \033[1mAllow? [y/N/message]: \033[0m").strip()
            except EOFError:
                answer = "n"

    if answer.lower() in ("y", "yes"):
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
        log_msg("approval", {"action": "allow", "tool": tool_name, "input": tool_input})
    elif answer.lower() in ("n", "no", ""):
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {
                    "behavior": "deny",
                    "message": "User denied permission",
                },
            },
        }
        log_msg("approval", {"action": "deny", "tool": tool_name})
    else:
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {
                    "behavior": "deny",
                    "message": answer,
                },
            },
        }
        log_msg("approval", {"action": "deny", "tool": tool_name, "message": answer})

    log_msg("send", response)
    send_to_claude(proc, response)


# ---------------------------------------------------------------------------
# Telegram bot (runs in its own thread)
# ---------------------------------------------------------------------------

def run_telegram_bot(loop):
    """Run the Telegram bot in a separate thread with its own event loop."""
    asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(TOKEN).build()

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        username = update.effective_user.username
        if username not in WHITELIST:
            logger.warning("Rejected from non-whitelisted user: %s", username)
            return

        chat_id = update.effective_chat.id
        text = update.message.text or ""
        if not text:
            return

        log_msg("telegram_in", {"username": username, "chat_id": chat_id, "text": text})

        # Send status message
        status = await context.bot.send_message(chat_id=chat_id, text="🤖 Thinking...")

        # Update shared state
        telegram_state["bot"] = context.bot
        telegram_state["chat_id"] = chat_id
        telegram_state["status_msg_id"] = status.message_id
        telegram_state["stream_lines"] = []

        # Put message in queue for main thread to send to Claude
        telegram_msg_queue.put({"type": "user_message", "text": text})

    async def handle_permission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        username = query.from_user.username
        if username not in WHITELIST:
            await query.answer("Not authorized")
            return

        data = query.data
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[0] != "perm":
            await query.answer("Invalid")
            return

        action = parts[1]
        request_id = parts[2]
        allow = action == "allow"

        log_msg("telegram_approval", {"username": username, "action": action, "request_id": request_id})

        # Put permission response in queue for main thread
        telegram_msg_queue.put({
            "type": "permission_response",
            "request_id": request_id,
            "allow": allow,
        })

        emoji = "✅ Allowed" if allow else "❌ Denied"
        try:
            await query.edit_message_text(
                text=query.message.text + f"\n\n{emoji} by @{username}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        await query.answer(emoji)

        # Update shared state
        telegram_state["bot"] = context.bot

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_permission_callback))

    async def run():
        async with app:
            await app.start()
            await app.updater.start_polling()
            logger.info("Telegram bot ready. Waiting for messages...")
            # Run forever
            stop_event = asyncio.Event()
            await stop_event.wait()

    loop.run_until_complete(run())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"\033[90m📝 Logging to: {LOG_PATH}\033[0m")
    print("Type your messages below. Also accepting messages via Telegram.")
    print("Ctrl+C or Ctrl+D to exit.\n")

    # Start Telegram bot in background thread
    tg_loop = asyncio.new_event_loop()
    telegram_state["loop"] = tg_loop
    tg_thread = threading.Thread(target=run_telegram_bot, args=(tg_loop,), daemon=True)
    tg_thread.start()

    # Start Claude process
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    import subprocess
    proc = subprocess.Popen(
        [
            "claude",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--permission-prompt-tool", "stdio",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
    )

    logger.info("Claude process started (pid=%d)", proc.pid)

    # Read output in background thread
    reader = threading.Thread(target=read_output, args=(proc,), daemon=True)
    reader.start()

    try:
        while True:
            # Check for pending permission requests
            try:
                perm_msg = permission_queue.get(timeout=0.05)
                handle_permission(proc, perm_msg)
                continue
            except queue.Empty:
                pass

            # Check for Telegram messages
            try:
                tg_msg = telegram_msg_queue.get(timeout=0.05)
                if tg_msg.get("type") == "user_message":
                    text = tg_msg["text"]
                    print(f"\n\033[35m📱 Telegram:\033[0m {text}")

                    msg = {
                        "type": "user",
                        "session_id": "",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": text}],
                        },
                        "parent_tool_use_id": None,
                    }
                    log_msg("send", msg)
                    send_to_claude(proc, msg)
                # permission_response is handled in handle_permission()
                continue
            except queue.Empty:
                pass

            # Check if process is still running
            if proc.poll() is not None:
                break

            # Check for terminal input
            if select.select([sys.stdin], [], [], 0.1)[0]:
                try:
                    user_input = input("\n✏️  You: ").strip()
                except EOFError:
                    break
                if not user_input:
                    continue

                msg = {
                    "type": "user",
                    "session_id": "",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": user_input}],
                    },
                    "parent_tool_use_id": None,
                }
                log_msg("send", msg)
                send_to_claude(proc, msg)

                # Set up Telegram status if we have a chat
                if telegram_state["bot"] and telegram_state["chat_id"]:
                    telegram_state["stream_lines"] = []

                    async def _send_status():
                        try:
                            status = await telegram_state["bot"].send_message(
                                chat_id=telegram_state["chat_id"],
                                text="🤖 Thinking...",
                            )
                            telegram_state["status_msg_id"] = status.message_id
                        except Exception:
                            pass

                    future = asyncio.run_coroutine_threadsafe(_send_status(), tg_loop)
                    try:
                        future.result(timeout=5)
                    except Exception:
                        pass

    except KeyboardInterrupt:
        print("\n\n[Interrupted]")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(f"\n\033[90m📝 Full log saved to: {LOG_PATH}\033[0m")


if __name__ == "__main__":
    main()
