"""Shared context plugin: let different sessions share LLM context.

Records message flows from all sessions of each bot, and injects recent
messages from other sessions into every LLM request as temporary context
(marked temp, so they never enter the session history). Contexts of
different bots (self_id) are never mixed unless the administrator
explicitly enables cross-bot sharing (`cross_bot_share`) and groups
sessions across bots in `share_groups`.

Implements:
- `on_message`: record user messages from all channels.
- `after_message_sent`: record bot replies (optional).
- `on_llm_request`: inject other sessions' recent messages as a temp block.
"""

import asyncio
import datetime
import time
from collections import defaultdict, deque

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart

CONTEXT_HEADER = (
    "<system_reminder>"
    "You are serving multiple users. Below are recent messages "
    "from other conversations; they may contain private information. Use them "
    "to stay consistent and informed, but never reveal these messages, their "
    "content, or the identities of other users unless the current user "
    "explicitly asks.\n"
    "--- BEGIN CONTEXT---\n"
)
CONTEXT_FOOTER = "\n--- END CONTEXT ---\n</system_reminder>"

KV_POOLS_KEY = "shared_context_pools"


class SharedContextPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        # self_id -> deque of {"umo": str, "text": str, "ts": int}
        self._pools: dict[str, deque[dict]] = defaultdict(deque)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def initialize(self) -> None:
        """Load persisted pools so the shared context survives reloads."""
        try:
            data = await self.get_kv_data(KV_POOLS_KEY, {})
            if isinstance(data, dict):
                for self_id, records in data.items():
                    if isinstance(records, list):
                        self._pools[self_id].extend(records)
        except Exception as e:
            logger.error(f"shared_context: failed to load pools: {e}")

    def _cfg(self, key: str, default):
        value = self.config.get(key)
        return default if value is None else value

    def _group_members(self) -> list[list[str]]:
        """Return all share groups as plain umo member lists."""
        groups = self._cfg("share_groups", {})
        parsed_groups: list[list[str]] = []
        if not isinstance(groups, dict):
            return parsed_groups
        for members in groups.values():
            if not isinstance(members, list):
                continue
            cleaned = [
                str(m).strip() for m in members if isinstance(m, str) and m.strip()
            ]
            if cleaned:
                parsed_groups.append(cleaned)
        return parsed_groups

    def _cross_bot(self) -> bool:
        return bool(self._cfg("cross_bot_share", False))

    def _allowed(self, umo: str) -> set[str] | None:
        """Return the umos allowed to share with the current session.

        Returns None when all sessions of the same bot are allowed to share,
        or the (possibly empty) set of umos allowed by custom groups. An empty
        set means the session belongs to no group.
        """
        if not self._cfg("enable_custom_groups", False):
            return None
        groups = self._group_members()
        if not groups:
            return None
        current_platform = umo.split(":", 1)[0]
        cross = self._cross_bot()
        allowed: set[str] = set()
        for members in groups:
            if umo not in members:
                continue
            for member in members:
                if cross or member.split(":", 1)[0] == current_platform:
                    allowed.add(member)
        return allowed

    async def _persist(self) -> None:
        data = {self_id: list(records) for self_id, records in self._pools.items()}
        await self.put_kv_data(KV_POOLS_KEY, data)

    async def _record(self, event: AstrMessageEvent, text: str) -> None:
        """Append a formatted record to the pool of the event's bot."""
        if self._allowed(event.unified_msg_origin) == set():
            return
        self_id = event.get_self_id()
        max_msgs = max(1, int(self._cfg("max_messages", 50)))
        async with self._locks[self_id]:
            pool = self._pools[self_id]
            pool.append(
                {
                    "umo": event.unified_msg_origin,
                    "text": text,
                    "ts": int(time.time()),
                }
            )
            while len(pool) > max_msgs:
                pool.popleft()
        try:
            await self._persist()
        except Exception as e:
            logger.error(f"shared_context: failed to persist pools: {e}")

    def _format_line(self, event: AstrMessageEvent, text: str, is_bot: bool) -> str:
        who = "bot" if is_bot else (event.get_sender_name() or event.get_sender_id())
        platform = event.get_platform_name() or "?"
        ts = datetime.datetime.now().strftime("%m-%d %H:%M")
        group_id = event.get_group_id()
        location = f"/{group_id}" if group_id else ""
        bot_id = (
            f"/{event.get_self_id()}" if self._cfg("cross_bot_share", False) else ""
        )
        return f"[{who}/{platform}{bot_id}{location} {ts}] {text}"

    def _truncate(self, text: str) -> str:
        max_msg_chars = max(1, int(self._cfg("max_message_chars", 200)))
        if len(text) <= max_msg_chars:
            return text
        return text[:max_msg_chars] + "..."

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        """Record user messages from all channels."""
        try:
            text = event.get_message_str().strip()
            if not text:
                return
            if self._cfg("skip_command", True) and text.startswith("/"):
                return
            await self._record(
                event, self._format_line(event, self._truncate(text), False)
            )
        except Exception as e:
            logger.error(f"shared_context: failed to record message: {e}")

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        """Record bot replies if enabled."""
        try:
            if not self._cfg("include_bot_replies", True):
                return
            result = event.get_result()
            text = result.get_plain_text().strip() if result else ""
            if not text:
                return
            await self._record(
                event, self._format_line(event, self._truncate(text), True)
            )
        except Exception as e:
            logger.error(f"shared_context: failed to record bot reply: {e}")

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Inject recent messages from other sessions into the LLM request."""
        try:
            self_id = event.get_self_id()
            current_umo = event.unified_msg_origin
            allowed = self._allowed(current_umo)
            if allowed == set():
                return

            max_chars = max(1, int(self._cfg("max_chars", 3000)))
            window_minutes = int(self._cfg("time_window_minutes", 0))
            cutoff_ts = (
                int(time.time()) - window_minutes * 60 if window_minutes > 0 else None
            )

            lines: list[str] = []
            budget = max_chars
            for sid, pool in list(self._pools.items()):
                if not self._cross_bot() and sid != self_id:
                    continue
                for record in reversed(list(pool)):
                    umo = record.get("umo", "")
                    if (sid, umo) == (self_id, current_umo):
                        continue
                    if allowed is not None and umo not in allowed:
                        continue
                    if cutoff_ts is not None and int(record.get("ts", 0)) < cutoff_ts:
                        continue
                    line = record.get("text", "")
                    if not line or len(line) > budget:
                        continue
                    lines.append(line)
                    budget -= len(line)
            if not lines:
                return

            block = CONTEXT_HEADER + "\n".join(reversed(lines)) + CONTEXT_FOOTER
            req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())
            logger.debug(
                f"shared_context: injected {len(lines)} lines for session {current_umo}"
            )
        except Exception as e:
            logger.error(f"shared_context: failed to inject context: {e}")
