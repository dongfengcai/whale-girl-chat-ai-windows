"""
astrbot_plugin_identity -- 身份注入

在每条消息的提示词前面标注「谁在说话」，让模型能区分群里的不同用户。

背景
----
AstrBot 送给模型的提示词只有消息文本本身：

    今天天气不错

模型看不出这句话是谁说的。会话隔离（unique_session）能分开上下文，但代价是
机器人失去群聊的公共视野（A 说的话 B 问起时它不知道）。本插件用的是另一条路：
**不隔离上下文，只标注身份** —— 机器人能看到整个群的对话流，同时知道每句话
出自谁。

    今天天气不错            ->  （小明·群主）今天天气不错

角色从哪来
----------
群主/管理员只能通过平台接口查（OneBot 的 get_group_member_info，返回 role 字段：
owner / admin / member）。接口调用有几个必须处理好的问题：

* **不能每条消息都查** —— 一次往返几十到几百毫秒，会让回复变慢，2GB 机器更吃紧。
  所以按「群×用户」缓存，默认 10 分钟。
* **可能失败或超时** —— NapCat 没响应、机器人权限不足、用户已退群，都会失败。
  所以查询包了超时和异常兜底：**查不到就只标注名字**，绝不阻塞消息。
* **机器人不是管理员时可能拿不到部分字段** —— 同样退化为名字。

活跃度
------
平台接口不提供这个，只能本地统计。插件记录「群×用户」的发言时间戳，
滑动窗口内发言数达到阈值就标记「活跃」。状态在内存里，重启即清零 ——
这是有意为之：活跃度是短期信号，不值得为它引入落盘开销。

与其它插件的关系
----------------
本插件用 on_llm_request 钩子修改提示词，**不碰消息链**，所以和
poke_react / media_pacer / reply_arbiter 都不冲突 —— 那几个改的是消息内容，
本插件改的是最后送给模型的那段文字。
"""

import asyncio
import time
from collections import defaultdict

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

# (group_id, user_id) -> (时间戳, 角色文本)
_ROLE_CACHE: dict[tuple[str, str], tuple[float, str]] = {}

# group_id -> {user_id: [时间戳, ...]}
_ACTIVITY: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

ROLE_LABEL = {
    "owner": "群主",
    "admin": "管理员",
    # 普通成员也标注：标注"成员"能让模型明确知道「这是个普通群友」，
    # 从而和群主/管理员区别对待。不标的话它无从判断对方是不是管事的。
    "member": "成员",
}


class Identity(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    async def initialize(self):
        parts = ["名字"]
        if self.config.get("include_role", True):
            parts.append("角色")
        if self.config.get("include_group", False):
            parts.append("群名")
        if self.config.get("include_activity", False):
            parts.append("活跃度")
        logger.info(
            f"[identity] 启动：注入 {'+'.join(parts)}"
            f"，角色缓存 {self.config.get('role_cache_minutes', 10)} 分钟"
        )

    # ------------------------------------------------------------------ 角色查询
    @staticmethod
    def _role_from_raw(event: AstrMessageEvent) -> str:
        """
        从原始事件里直接取 role。

        OneBot v11 的群消息事件结构是：
            {"post_type":"message","message_type":"group",
             "sender":{"user_id":..., "nickname":"...", "role":"owner"|"admin"|"member"},
             ...}

        所以大多数情况下**根本不用调接口** —— 角色就在事件里。
        这是首选来源：零延迟、零失败率。
        """
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return ""
        sender = raw.get("sender")
        if not isinstance(sender, dict):
            return ""
        role = str(sender.get("role") or "").strip().lower()
        return role if role in ("owner", "admin", "member") else ""

    async def _role_from_api(self, event: AstrMessageEvent, group_id: str,
                             user_id: str) -> str:
        """
        调 OneBot 接口查角色（get_group_member_info）。

        作为 raw 事件的兜底 —— 有些适配器可能不在事件里带 sender.role。
        失败时抛异常，由调用方记日志。
        """
        platform_id = ""
        try:
            platform_id = event.get_platform_id()
        except Exception as e:
            raise RuntimeError(f"取 platform_id 失败: {e}") from e

        try:
            inst = self.context.get_platform_inst(platform_id)
        except Exception as e:
            raise RuntimeError(f"get_platform_inst({platform_id!r}) 抛错: {e}") from e

        if inst is None:
            raise RuntimeError(
                f"get_platform_inst({platform_id!r}) 返回 None "
                f"（已注册平台：{[type(x).__name__ for x in getattr(self.context.platform_manager, 'platform_insts', [])]}）"
            )

        bot = getattr(inst, "bot", None)
        if bot is None:
            raise RuntimeError(
                f"{type(inst).__name__} 上没有 .bot 属性"
                f"（可用属性：{[a for a in dir(inst) if not a.startswith('_')][:20]}）"
            )

        action = getattr(bot, "call_action", None)
        if not callable(action):
            raise RuntimeError(f"{type(bot).__name__} 上没有可调用的 call_action")

        info = await action(
            "get_group_member_info",
            group_id=int(group_id),
            user_id=int(user_id),
            no_cache=False,
        )
        if not isinstance(info, dict):
            raise RuntimeError(f"接口返回非字典：{type(info).__name__} {info!r}")

        role = str(info.get("role") or "").strip().lower()
        if not role:
            raise RuntimeError(f"接口返回里没有 role 字段（返回键：{list(info)[:15]}）")
        return role

    async def _query_role(self, event: AstrMessageEvent, group_id: str,
                          user_id: str) -> str:
        """
        取群角色，三级兜底：raw 事件 → 接口 → 空。

        每一步失败都记日志 —— 之前这里把异常全吞了，导致"只有名字没有角色"
        时完全看不出原因，这个教训值得留着。
        """
        cache_min = max(1, int(self.config.get("role_cache_minutes", 10) or 10))
        key = (group_id, user_id)
        debug = self.config.get("debug", False)

        hit = _ROLE_CACHE.get(key)
        if hit and (time.time() - hit[0]) < cache_min * 60:
            return hit[1]

        # ① 首选：直接从事件里取（无需接口调用）
        role = self._role_from_raw(event)
        source = "raw事件"

        # ② 兜底：调接口
        if not role:
            timeout = max(1, int(self.config.get("query_timeout_seconds", 3) or 3))
            try:
                role = await asyncio.wait_for(
                    self._role_from_api(event, group_id, user_id),
                    timeout=timeout,
                )
                source = "接口"
            except asyncio.TimeoutError:
                logger.warning(
                    f"[identity] 查角色超时（{timeout}s），本次只标注名字。"
                    f"group={group_id} user={user_id}"
                )
            except Exception as e:
                logger.warning(
                    f"[identity] 查角色失败，本次只标注名字：{e} "
                    f"group={group_id} user={user_id}"
                )

        if not role and debug:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            logger.info(
                f"[identity] 角色为空。raw_message 类型={type(raw).__name__}"
                + (f" 键={list(raw)[:15]}" if isinstance(raw, dict) else "")
            )

        # 只缓存「查到了角色」的结果。
        #
        # 空结果**不能**缓存：一次超时或接口报错如果被缓存 10 分钟，该用户接下来
        # 10 分钟都只有名字，而且连"查角色失败"的日志都不会打（压根没去查）——
        # 表现就是"某个人突然一直没角色"，且毫无提示。宁可每次重试。
        if role:
            _ROLE_CACHE[key] = (time.time(), role)
        else:
            _ROLE_CACHE.pop(key, None)

        if debug and role:
            logger.info(f"[identity] 角色={role}（来源：{source}）")
        return role

    # ------------------------------------------------------------------ 活跃度
    def _note_activity(self, group_id: str, user_id: str):
        if not group_id or not user_id:
            return
        window_h = max(1, int(self.config.get("activity_window_hours", 72) or 72))
        cutoff = time.time() - window_h * 3600
        bucket = _ACTIVITY[group_id][user_id]
        bucket.append(time.time())
        # 顺手裁掉过期的，避免列表无限增长
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)

    def _is_active(self, group_id: str, user_id: str) -> bool:
        if not group_id or not user_id:
            return False
        need = max(1, int(self.config.get("activity_min_messages", 10) or 10))
        window_h = max(1, int(self.config.get("activity_window_hours", 72) or 72))
        cutoff = time.time() - window_h * 3600
        times = _ACTIVITY.get(group_id, {}).get(user_id) or []
        return sum(1 for t in times if t >= cutoff) >= need

    # ------------------------------------------------------------------ 注入
    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.config.get("enabled", True):
            return

        try:
            user_id = str(event.get_sender_id())
        except Exception:
            return

        group_id = ""
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            group_id = ""

        is_private = True
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            pass

        # 统计活跃度（私聊也记，但只在群里用）
        if group_id:
            self._note_activity(group_id, user_id)

        # 拼标签
        parts: list[str] = []

        if self.config.get("include_name", True):
            name = ""
            try:
                name = (event.get_sender_name() or "").strip()
            except Exception:
                name = ""
            if not name:
                name = user_id
            limit = max(1, int(self.config.get("name_max_length", 12) or 12))
            if len(name) > limit:
                name = name[:limit] + "…"
            parts.append(name)

        if self.config.get("include_role", True) and group_id:
            role = await self._query_role(event, group_id, user_id)
            label = ROLE_LABEL.get(role)
            if label:
                parts.append(label)

        if self.config.get("include_group", False) and group_id:
            try:
                gname = ""
                inst = None
                try:
                    inst = self.context.get_platform_inst(event.get_platform_id())
                except Exception:
                    inst = None
                if inst is not None:
                    get_group = getattr(inst, "get_group", None)
                    if callable(get_group):
                        g = await get_group(group_id=group_id)
                        gname = str(getattr(g, "group_name", "") or "").strip()
                if gname:
                    limit = max(1, int(self.config.get("name_max_length", 12) or 12))
                    if len(gname) > limit:
                        gname = gname[:limit] + "…"
                    parts.append(f"群:{gname}")
            except Exception:
                pass

        if self.config.get("include_activity", False) and group_id:
            if self._is_active(group_id, user_id):
                parts.append("活跃")

        if not parts:
            return

        tag = "·".join(parts)

        # 私聊不加群角色的空壳；群聊里标注才有多人区分的意义
        if is_private and len(parts) == 1 and not self.config.get("include_name", True):
            return

        prompt = req.prompt or ""
        # 避免重复注入（同一请求可能被多次处理）
        if prompt.startswith(f"（{tag}）"):
            return

        try:
            req.prompt = f"（{tag}）{prompt}"
        except Exception as e:
            logger.warning(f"[identity] 注入失败：{e}")
            return

        if self.config.get("debug", False):
            logger.info(f"[identity] 注入 -> （{tag}）{prompt[:40]}")
