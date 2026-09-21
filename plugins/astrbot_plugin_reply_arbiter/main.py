"""
astrbot_plugin_reply_arbiter -- 多人 @ 时的回应仲裁

需求 7：
    如果群聊同时被多人 @，则根据上下文优先回应之前一直在聊的，把之前的内容
    接下去，否则回应最先 @ 的。

需求 6（群聊需 @、私聊免 @）是 AstrBot 的内置行为，本插件只做兜底校验：
确实存在 `At(self_id)` 才走仲裁，避免把 wake_prefix（默认 `/`）触发的指令
误判成 @。

实现思路（以及为什么不是"合并成一次回复"）
------------------------------------------
AstrBot 的流水线是**每条消息独立处理**的，"把三条 @ 合并进一次 LLM 调用"需要
往上下文里注入人造消息，属于对内部流水线的深度改写，版本一升级就会碎。

这里采用**不改消息内容**的等效方案：

    合并窗口内，只有优先级最高的那条 @ 放行给 LLM，其余用
    stop_event() + should_call_llm(True) 静默拦下。

用户观感上是一回事：多人同时 @ 时只有一个人得到回复，而且回的是上下文里
正在聊的那个人。差别只在于模型看到的是那一条消息、而不是三条拼起来的。

优先级判定
----------
候选人在「上下文回看窗口」内满足任一条件即为高优先级：
  * 机器人在最近若干条回复里回应过 TA
  * TA 在窗口内发言次数达到 active_message_count

两者都不满足 -> 全部按低优先级处理，走"回应最早 @"。

关于"机器人回应过 TA"的判定
---------------------------
AstrBot 没有开放「这条回复是回给谁的」的回调，所以这里用 `after_message_sent`
钩子记录「机器人刚在哪个会话发过言」，再回填给该会话当时的候选人在聊对象。
这是一个**启发式近似**，在多人同时 @ 的场景下足够准；如果判错，退化结果只是
"回应最早 @"，不会出错也不会卡住。
"""

import asyncio
import contextlib
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star

# 会话 -> {"speakers": {uid: [ts, ...]}, "responded": [(ts, uid)], "last_target": uid}
_STATE: dict[str, dict] = {}

# 会话 -> {"event", "sender", "priority", "gen"}
_DEFER: dict[str, dict] = {}

# 会话 -> {"sender": uid, "ts": float}
# 记录"最近一次仲裁放行给谁"，供 after_message_sent 记账用。
# 带时间戳是必需的：回复是异步发出的，on_sent 触发时无法保证是同一轮的放行；
# 超过 _RELEASE_TTL 秒的记录不再参与归属，避免把回复错误算到上一轮的人头上。
_LAST_RELEASE: dict[str, dict] = {}
_RELEASE_TTL = 60.0


def _now() -> float:
    return time.time()


def _group_key(event: AstrMessageEvent) -> str:
    try:
        gid = event.get_group_id()
        if gid:
            return f"group:{gid}"
    except Exception:
        pass
    try:
        return f"private:{event.get_sender_id()}"
    except Exception:
        return "unknown"


def _sender(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_id())
    except Exception:
        return ""


def _st(event: AstrMessageEvent) -> dict:
    key = _group_key(event)
    s = _STATE.get(key)
    if s is None:
        s = {"speakers": {}, "responded": [], "last_target": ""}
        _STATE[key] = s
    return s


class ReplyArbiter(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    async def initialize(self):
        logger.info(
            "[reply_arbiter] 启动：合并窗口 "
            f"{self.config.get('merge_window_ms', 2000)}ms，"
            f"上下文回看 {self.config.get('context_window_seconds', 300)}s"
        )

    # ------------------------------------------------------------------ 工具
    def _mentioned(self, event: AstrMessageEvent) -> bool:
        """是否真的 @ 了机器人（不是 wake_prefix 触发的）。"""
        try:
            me = str(event.get_self_id())
        except Exception:
            return False
        try:
            for seg in event.get_messages():
                if isinstance(seg, At) and str(getattr(seg, "qq", "")) == me:
                    return True
        except Exception:
            return False
        return False

    def _prune(self, s: dict, window: float):
        cutoff = _now() - window
        for uid in list(s["speakers"].keys()):
            times = [t for t in s["speakers"][uid] if t >= cutoff]
            if times:
                s["speakers"][uid] = times
            else:
                del s["speakers"][uid]
        s["responded"] = [(t, u) for (t, u) in s["responded"] if t >= cutoff]

    def _classify(self, s: dict, uid: str, window: float) -> int:
        """0 = 低优先级，1 = 高优先级（之前一直在聊）。"""
        need = max(1, int(self.config.get("active_message_count", 2)))
        if len(s["speakers"].get(uid, [])) >= need:
            return 1
        if any(u == uid for (_t, u) in s["responded"]):
            return 1
        return 0

    def _record_speaker(self, event: AstrMessageEvent):
        uid = _sender(event)
        if not uid:
            return
        s = _st(event)
        s["speakers"].setdefault(uid, []).append(_now())
        self._prune(s, float(self.config.get("context_window_seconds", 300)))

    # ------------------------------------------------------------------ 主线
    @filter.event_message_type(filter.EventMessageType.ALL, priority=998)
    async def arbitrate(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True):
            return

        is_private = True
        with contextlib.suppress(Exception):
            is_private = bool(event.is_private_chat())

        if is_private and not self.config.get("arbitrate_in_private", False):
            return

        # 群聊必须真的 @ 到机器人才参与仲裁（需求 6 兜底）
        if not is_private and self.config.get("require_mention_in_group", True):
            if not self._mentioned(event):
                # 没 @ 就不是唤醒，但发言仍然计入上下文
                self._record_speaker(event)
                return

        uid = _sender(event)
        if not uid:
            return

        key = _group_key(event)

        # 这一条是不是刚刚被仲裁放行的？是则记账并放行
        rel = _LAST_RELEASE.get(key)
        if rel and rel.get("sender") == uid and (_now() - rel.get("ts", 0)) <= _RELEASE_TTL:
            _LAST_RELEASE.pop(key, None)
            s = _st(event)
            self._record_speaker(event)
            if self.config.get("debug", True):
                logger.info(f"[reply_arbiter] {key} 放行 {uid} 交给模型")
            return

        s = _st(event)
        window = float(self.config.get("context_window_seconds", 300))
        self._prune(s, window)
        priority = self._classify(s, uid, window)
        self._record_speaker(event)

        # 管理员免仲裁
        if self.config.get("bypass_admin", True):
            try:
                if event.is_admin():
                    # 取消正在等待的候选人，管理员立即放行
                    cur = _DEFER.pop(key, None)
                    if cur is not None:
                        self._suppress(cur["event"])
                        if self.config.get("debug", True):
                            logger.info(
                                f"[reply_arbiter] {key} 管理员插队，"
                                f"取消 {cur['sender']} 的等待"
                            )
                    if self.config.get("debug", True):
                        logger.info(f"[reply_arbiter] {key} 管理员 {uid} 直接放行")
                    return
            except Exception:
                pass

        wait_ms = max(0, int(self.config.get("merge_window_ms", 2000)))
        if wait_ms == 0:
            return  # 不等待，直接放行

        cur = _DEFER.get(key)

        if cur is None:
            # 还没有人在等 -> 我成为候选人，开窗等待
            await self._defer(key, event, uid, priority, wait_ms)
            return

        if priority > cur["priority"]:
            # 新来的更该被回应（TA 才是在聊的人）：
            # 让位者已经在它自己的窗口里等待，代码里直接让它超时退出，
            # 这里只需要把它从候选表里摘掉并接管。
            _DEFER.pop(key, None)
            if self.config.get("debug", True):
                logger.info(
                    f"[reply_arbiter] {key} {uid}（优先级 {priority}）"
                    f"取代在等的 {cur['sender']}（优先级 {cur['priority']}）"
                )
            # 让位者退出前不会被抑制 —— 它会在 _defer 里发现 gen 不匹配而静默返回，
            # 但那会让它继续走流水线。所以必须显式抑制。
            self._suppress(cur["event"])
            await self._defer(key, event, uid, priority, wait_ms)
            return

        # 新来的优先级不高于在等的 -> 保留最早 @ 的（需求 7）
        self._suppress(event)
        if self.config.get("debug", True):
            logger.info(
                f"[reply_arbiter] {key} {uid} 让位给 {cur['sender']}"
                f"（{cur['priority']} >= {priority}）"
            )

    # ------------------------------------------------------------------ 延迟放行
    async def _defer(self, key: str, event: AstrMessageEvent, uid: str,
                     priority: int, wait_ms: int):
        """
        在 handler 内部等待，让这条事件保持在"处理中"。

        关键：AstrBot 的流水线会 await 本协程，所以 sleep 期间这条消息既没有
        被放行给 LLM，也没有结束。窗口结束后再决定放行还是抑制。

        如果这里改成"注册一个定时器然后立刻 return"，流水线会在 return 的那一刻
        就把消息送给 LLM，之后就再也拦不住了 —— 这是本插件唯一必须遵守的约束。
        """
        gen = {"n": 0}
        _DEFER[key] = {
            "event": event,
            "sender": uid,
            "priority": priority,
            "gen": gen,
        }
        if self.config.get("debug", True):
            logger.info(
                f"[reply_arbiter] {key} 收到 @ {uid}"
                f"（优先级 {priority}），开窗 {wait_ms}ms"
            )
        await asyncio.sleep(wait_ms / 1000.0)

        cur = _DEFER.get(key)
        if cur is None or cur.get("gen") is not gen:
            # 窗口期间被更高优先级的 @ 取代 -> 安静退出，让位者已经抑制过了
            return

        _DEFER.pop(key, None)
        _LAST_RELEASE[key] = {"sender": uid, "ts": _now()}
        if self.config.get("debug", True):
            logger.info(
                f"[reply_arbiter] {key} 窗口结束，放行 {uid} 交给模型"
            )

    # ------------------------------------------------------------------ 抑制
    def _suppress(self, target: AstrMessageEvent):
        """把这条消息拦在 LLM 之前（同步，无需 await）。"""
        try:
            target.stop_event()
            target.should_call_llm(True)  # 名字是反的：True = 不调用 LLM
        except Exception as e:
            logger.warning(f"[reply_arbiter] 抑制消息失败：{e}")
            return
        hint = str(self.config.get("suppressed_hint", "") or "").strip()
        if hint:
            with contextlib.suppress(Exception):
                asyncio.create_task(target.send(target.plain_result(hint)))

    # ------------------------------------------------------------------ 回应记账
    @filter.after_message_sent()
    async def on_sent(self, event: AstrMessageEvent):
        """
        机器人刚发过言 —— 记住这个会话当前的"在聊对象"。

        启发式说明：AstrBot 没有开放「这条回复回给谁」的回调，所以用最近一次
        仲裁选中的对象来近似。判错的后果只是退化成"回应最早 @"，不会出错。
        """
        if not self.config.get("enabled", True):
            return
        key = _group_key(event)
        rel = _LAST_RELEASE.get(key)
        if not rel:
            return
        # 太旧的记录不参与归属，避免回复被算到上一轮的人头上
        if (_now() - rel.get("ts", 0)) > _RELEASE_TTL:
            return
        uid = rel.get("sender") or ""
        if not uid:
            return
        s = _STATE.get(key)
        if not s:
            return
        s["responded"].append((_now(), uid))
        with contextlib.suppress(Exception):
            self._prune(s, float(self.config.get("context_window_seconds", 300)))
