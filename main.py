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
from typing import Literal

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
        groups = self._group_members()
        logger.info(
            "shared_context: initialized | cross_bot_share=%s | groups=%d | "
            "pool_sessions=%d",
            self._cross_bot(),
            len(groups),
            len(self._pools),
        )

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

    @staticmethod
    def _entry_matches(entry: str, umo: str, current_platform: str) -> bool:
        """Check whether a group entry covers the given session.

        Supported entry forms: exact umo (`qq-bot:FriendMessage:10001`),
        current-bot wildcard (`*`), and platform wildcard (`qq-bot:*`).
        """
        if entry == "*":
            return True
        if entry.endswith(":*"):
            return entry[:-2] == current_platform
        return entry == umo

    def _entry_allowed(self, entry: str, current_platform: str, cross: bool) -> bool:
        """Check whether a group entry is usable by the current session."""
        if entry == "*":
            return True
        if entry.endswith(":*"):
            return cross or entry[:-2] == current_platform
        return cross or entry.split(":", 1)[0] == current_platform

    def _in_any_group(self, umo: str) -> bool:
        """Check whether the session identified by umo is a group member."""
        platform = umo.split(":", 1)[0]
        return any(
            self._entry_matches(m, umo, platform)
            for members in self._group_members()
            for m in members
        )

    def _allowed(
        self, umo: str
    ) -> set[tuple[str, str]] | Literal["bot-out", "global"] | None:
        """Return the share rules allowed for the current session.

        Rules are ("umo", umo_str) exact sessions or ("platform", platform_id)
        wildcards (a bare "*" is normalized to ("platform", current_platform)).
        Sharing is symmetric: sessions inside a group only see each other, and
        sessions outside every group only see other outside-group sessions
        (never group members, and never seen by them).

        Returns:
            - None: all sessions of the same bot are allowed (default mode).
            - "bot-out": the session is outside every group and `bot` fallback
              is set; it may see unlisted same-bot sessions only.
            - "global": the session is outside every group and `global`
              fallback is set; it may see unlisted sessions of every bot.
            - An empty set: the session is outside every group and isolated.
            - Otherwise: the rule set allowed by the session's groups.
        """
        if not self._cfg("enable_custom_groups", False):
            return None
        groups = self._group_members()
        if not groups:
            return None
        current_platform = umo.split(":", 1)[0]
        cross = self._cross_bot()
        allowed: set[tuple[str, str]] = set()
        for members in groups:
            if not any(self._entry_matches(m, umo, current_platform) for m in members):
                continue
            for member in members:
                if not self._entry_allowed(member, current_platform, cross):
                    continue
                if member == "*":
                    allowed.add(("platform", current_platform))
                elif member.endswith(":*"):
                    allowed.add(("platform", member[:-2]))
                else:
                    allowed.add(("umo", member))
        if allowed:
            return allowed
        mode = self._cfg("out_of_group_mode", "isolate")
        if mode == "bot":
            return "bot-out"
        if mode == "global":
            return "global"
        return set()

    async def _persist(self) -> None:
        data = {self_id: list(records) for self_id, records in self._pools.items()}
        await self.put_kv_data(KV_POOLS_KEY, data)

    async def _record(self, event: AstrMessageEvent, text: str) -> None:
        """Append a formatted record to the pool of the event's bot."""
        umo = event.unified_msg_origin
        if self._allowed(umo) == set():
            return
        self_id = event.get_self_id()
        max_msgs = max(1, int(self._cfg("max_messages", 50)))
        async with self._locks[self_id]:
            pool = self._pools[self_id]
            pool.append({"umo": umo, "text": text, "ts": int(time.time())})
            while len(pool) > max_msgs:
                pool.popleft()
        logger.debug(f"shared_context: recorded | {self_id} | {umo} | {text}")
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
            bot_out = allowed == "bot-out"
            global_share = allowed == "global"
            if bot_out or global_share:
                allowed = None
            logger.debug(
                f"shared_context: session hit | {current_umo} | "
                f"mode={'global' if global_share else 'bot-out' if bot_out else 'share_all' if allowed is None else 'rules(' + str(len(allowed)) + ')'} | "
                f"cross_bot={self._cross_bot()}"
            )

            max_chars = max(1, int(self._cfg("max_chars", 3000)))
            window_minutes = int(self._cfg("time_window_minutes", 0))
            cutoff_ts = (
                int(time.time()) - window_minutes * 60 if window_minutes > 0 else None
            )

            lines: list[str] = []
            budget = max_chars
            for sid, pool in list(self._pools.items()):
                if not global_share and not self._cross_bot() and sid != self_id:
                    continue
                before = len(lines)
                for record in reversed(list(pool)):
                    umo = record.get("umo", "")
                    if (sid, umo) == (self_id, current_umo):
                        continue
                    if (bot_out or global_share) and self._in_any_group(umo):
                        continue
                    if allowed is not None:
                        platform = umo.split(":", 1)[0]
                        matched = ("umo", umo) in allowed or (
                            "platform",
                            platform,
                        ) in allowed
                        if not matched:
                            continue
                    if cutoff_ts is not None and int(record.get("ts", 0)) < cutoff_ts:
                        continue
                    line = record.get("text", "")
                    if not line or len(line) > budget:
                        continue
                    lines.append(line)
                    budget -= len(line)
                if len(lines) > before:
                    logger.debug(
                        f"shared_context: pool hit | bot={sid} | "
                        f"contributed={len(lines) - before} lines"
                    )
            if not lines:
                logger.debug(
                    f"shared_context: pools empty or no match, skip injection | "
                    f"{current_umo}"
                )
                return

            block = CONTEXT_HEADER + "\n".join(reversed(lines)) + CONTEXT_FOOTER
            req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())
            logger.debug(
                f"shared_context: injected {len(lines)} lines for session {current_umo}"
            )
        except Exception as e:
            logger.error(f"shared_context: failed to inject context: {e}")
