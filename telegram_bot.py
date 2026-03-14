"""
telegram_bot.py — Telegram frontend for the multi-tenant Claude system.

Runs as a thread within the router. Handles all Telegram I/O:
- Receive user messages, forward to router via callbacks
- Send Claude responses, tool progress, permission prompts back to users
- Per-user state tracking (chat_id, streaming message, etc.)
"""

import asyncio
import logging
import threading
from typing import Callable, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

logger = logging.getLogger("telegram-bot")


def _escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class TelegramBot:
    """Multi-user Telegram bot frontend.

    Callbacks:
        on_message(tg_user_id, tg_first_name, tg_username, text, chat_id)
        on_permission_response(tg_user_id, request_id, allow)
    """

    def __init__(
        self,
        token: str,
        whitelist_ids: set,
        on_message: Callable,
        on_permission_response: Callable,
    ):
        self.token = token
        self.whitelist_ids = whitelist_ids  # empty = allow all
        self.on_message = on_message
        self.on_permission_response = on_permission_response
        self.user_states: dict[int, dict] = {}  # tg_user_id -> state
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._bot = None  # set once the bot starts

    def start(self) -> threading.Thread:
        """Start bot in a background thread. Returns the thread."""
        self.loop = asyncio.new_event_loop()
        thread = threading.Thread(target=self._run, args=(self.loop,), daemon=True)
        thread.start()
        return thread

    def _is_authorized(self, tg_user_id: int) -> bool:
        if not self.whitelist_ids:
            return True
        return tg_user_id in self.whitelist_ids

    def _get_state(self, tg_user_id: int) -> dict:
        if tg_user_id not in self.user_states:
            self.user_states[tg_user_id] = {
                "chat_id": None,
                "bot": None,
                "status_msg_id": None,
                "stream_lines": [],
            }
        return self.user_states[tg_user_id]

    # ------------------------------------------------------------------
    # Methods called by router (from any thread)
    # ------------------------------------------------------------------

    def send_thinking(self, tg_user_id: int):
        """Send a 'Thinking...' status message for this user."""
        state = self._get_state(tg_user_id)
        bot = state.get("bot") or self._bot
        chat_id = state.get("chat_id")
        if not bot or not chat_id or not self.loop:
            return

        state["stream_lines"] = []

        async def _send():
            try:
                msg = await bot.send_message(chat_id=chat_id, text="Thinking...")
                state["status_msg_id"] = msg.message_id
            except Exception as e:
                logger.warning("send_thinking failed: %s", e)

        future = asyncio.run_coroutine_threadsafe(_send(), self.loop)
        try:
            future.result(timeout=10)
        except Exception:
            pass

    def send_text(self, tg_user_id: int, text: str):
        """Send a text message to a user."""
        state = self._get_state(tg_user_id)
        bot = state.get("bot") or self._bot
        chat_id = state.get("chat_id")
        if not bot or not chat_id or not self.loop:
            return

        async def _send():
            try:
                for i in range(0, len(text), 4000):
                    await bot.send_message(chat_id=chat_id, text=text[i:i + 4000])
            except Exception as e:
                logger.warning("send_text failed: %s", e)

        future = asyncio.run_coroutine_threadsafe(_send(), self.loop)
        try:
            future.result(timeout=10)
        except Exception:
            pass

    def update_stream(self, tg_user_id: int, line: str):
        """Append a line to the streaming status message."""
        state = self._get_state(tg_user_id)
        state["stream_lines"].append(line)

        bot = state.get("bot") or self._bot
        chat_id = state.get("chat_id")
        msg_id = state.get("status_msg_id")
        if not bot or not chat_id or not msg_id or not self.loop:
            return

        lines = state["stream_lines"]
        text = "Working...\n\n" + "\n".join(lines)
        if len(text) > 4000:
            text = text[:500] + "\n...\n" + text[-3400:]

        async def _edit():
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=text,
                )
            except Exception as e:
                if "message is not modified" not in str(e).lower():
                    logger.warning("update_stream failed: %s", e)

        future = asyncio.run_coroutine_threadsafe(_edit(), self.loop)
        try:
            future.result(timeout=10)
        except Exception:
            pass

    def finalize_stream(self, tg_user_id: int, stats: str):
        """Finalize the streaming message with stats."""
        state = self._get_state(tg_user_id)
        bot = state.get("bot") or self._bot
        chat_id = state.get("chat_id")
        msg_id = state.get("status_msg_id")
        if not bot or not chat_id or not msg_id or not self.loop:
            return

        lines = state["stream_lines"]
        status_line = stats if stats else "Done"
        if lines:
            final_text = "\n".join(lines) + "\n\n" + status_line
        else:
            final_text = status_line
        if len(final_text) > 4000:
            final_text = final_text[:500] + "\n...\n" + final_text[-3400:]

        async def _edit():
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=final_text,
                )
            except Exception as e:
                if "message is not modified" not in str(e).lower():
                    logger.warning("finalize_stream failed: %s", e)

        future = asyncio.run_coroutine_threadsafe(_edit(), self.loop)
        try:
            future.result(timeout=10)
        except Exception:
            pass

        state["status_msg_id"] = None
        state["stream_lines"] = []

    def send_permission_prompt(self, tg_user_id: int, request_id: str,
                                tool_name: str, details: dict):
        """Send a permission request with Allow/Deny buttons."""
        state = self._get_state(tg_user_id)
        bot = state.get("bot") or self._bot
        chat_id = state.get("chat_id")
        if not bot or not chat_id or not self.loop:
            return

        tg_lines = [f"<b>Permission requested</b>"]
        tg_lines.append(f"Tool: <code>{_escape(tool_name)}</code>")
        reason = details.get("reason", "")
        if reason:
            tg_lines.append(f"Reason: {_escape(reason)}")
        tool_input = details.get("tool_input", {})
        for key in ["file_path", "command", "url", "pattern", "description"]:
            if key in tool_input:
                val = str(tool_input[key])
                if len(val) > 200:
                    val = val[:200] + "..."
                tg_lines.append(f"{key}: <code>{_escape(val)}</code>")

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Allow", callback_data=f"perm:allow:{request_id}"),
                InlineKeyboardButton("Deny", callback_data=f"perm:deny:{request_id}"),
            ]
        ])

        async def _send():
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text="\n".join(tg_lines),
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.warning("send_permission_prompt failed: %s", e)

        future = asyncio.run_coroutine_threadsafe(_send(), self.loop)
        try:
            future.result(timeout=10)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal: bot event loop
    # ------------------------------------------------------------------

    def _run(self, loop: asyncio.AbstractEventLoop):
        asyncio.set_event_loop(loop)
        app = ApplicationBuilder().token(self.token).build()

        async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user = update.effective_user
            tg_user_id = user.id
            if not self._is_authorized(tg_user_id):
                logger.warning("Rejected from unauthorized user: %s (id=%d)", user.username, tg_user_id)
                await update.message.reply_text("Not authorized.")
                return

            chat_id = update.effective_chat.id
            text = update.message.text or ""
            if not text:
                return

            # Update per-user state
            state = self._get_state(tg_user_id)
            state["chat_id"] = chat_id
            state["bot"] = context.bot
            self._bot = context.bot

            # Send thinking status
            status = await context.bot.send_message(chat_id=chat_id, text="Thinking...")
            state["status_msg_id"] = status.message_id
            state["stream_lines"] = []

            # Callback to router
            self.on_message(
                tg_user_id,
                user.first_name or "",
                user.username or "",
                text,
                chat_id,
            )

        async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
            query = update.callback_query
            tg_user_id = query.from_user.id
            if not self._is_authorized(tg_user_id):
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

            # Update button text
            emoji = "Allowed" if allow else "Denied"
            try:
                await query.edit_message_text(
                    text=query.message.text + f"\n\n{emoji}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            await query.answer(emoji)

            # Update state
            state = self._get_state(tg_user_id)
            state["bot"] = context.bot
            self._bot = context.bot

            # Callback to router
            self.on_permission_response(tg_user_id, request_id, allow)

        app.add_handler(MessageHandler(filters.TEXT, handle_message))
        app.add_handler(CallbackQueryHandler(handle_callback))

        async def run():
            async with app:
                await app.start()
                await app.updater.start_polling()
                logger.info("Telegram bot ready")
                stop_event = asyncio.Event()
                await stop_event.wait()

        loop.run_until_complete(run())
