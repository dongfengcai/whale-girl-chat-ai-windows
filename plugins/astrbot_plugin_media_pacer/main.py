"""
astrbot_plugin_media_pacer -- 媒体节流与过滤

需求 4：能识别图片，但识别要有间隔，且不影响纯文字消息和 API 调用。
需求 5：不识别语音和视频，非文字非图片的消息直接跳过。

实现方式
--------
在 `priority=1000`（比 balance_guard 更早）的处理器里**改写消息链**：

* 语音 / 视频 / 文件 / 合并转发 -> 从链里移除。
  若移除后这条消息既没有文字也没有图片，则 `stop_event()` 把它拦在 LLM 之前，
  静默忽略，不做任何回复。
* 图片 -> 走令牌桶。有令牌放行；没令牌则移除图片但**保留文字**。
* 纯文字 -> 完全不干预。

为什么要改写消息链而不是"拦住整条消息"
--------------------------------------
需求 4 明确要求节流不能影响纯文字。一条消息常是「文字 + 图片」；简单拦截会连
文字一起丢掉，用户会以为机器人没听见。改写链之后文字照常进模型，只有图片被
拿掉 —— 这才是"不影响主要纯文字消息"。

关于消息链的直接修改
--------------------
AstrBot 的部分适配器通过 `get_messages()` 返回 `message_obj.message`，所以就地
赋回 `event.message_obj.message` 是生效的。为稳妥起见，改写失败时只记录日志、
不抛异常 —— 降级为"不节流"而不是让整条消息处理失败。

令牌桶
------
每个会话（群/私聊）一个桶，容量 capacity，每 refill_seconds 秒恢复 1 个。
惰性补充（读的时候按时间差补），不需要后台任务。
状态只在内存里，重启后从满桶开始 —— 这是有意的：限流是短期的抗抖动措施，
不是需要精确恢复的账本。
"""

import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import (
    At,
    AtAll,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.api.star import Context, Star

# 这些类型始终保留。
# Reply 保留是有意的：引用机器人回复本身就是一种唤醒方式。
_KEEP = (Plain, At, AtAll, Reply)

# 会话 -> {"tokens": float, "last": float}
_BUCKETS: dict[str, dict] = {}


class MediaPacer(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    async def initialize(self):
        logger.info(
            "[media_pacer] 启动：图片桶容量 "
            f"{self.config.get('image_capacity', 3)} 张，"
            f"每 {self.config.get('image_refill_seconds', 20)} 秒恢复 1 个"
        )

    # ------------------------------------------------------------------ 令牌桶
    def _capacity(self) -> int:
        return max(1, int(self.config.get("image_capacity", 3)))

    def _refill_seconds(self) -> int:
        return max(1, int(self.config.get("image_refill_seconds", 20)))

    def _take_tokens(self, key: str, want: int) -> int:
        """尝试取 want 个令牌，返回实际取到的数量（惰性补充）。"""
        capacity = self._capacity()
        refill = self._refill_seconds()
        now = time.time()

        b = _BUCKETS.get(key)
        if b is None:
            b = {"tokens": float(capacity), "last": now}
            _BUCKETS[key] = b

        elapsed = now - b["last"]
        if elapsed > 0:
            b["tokens"] = min(float(capacity), b["tokens"] + elapsed / refill)
            b["last"] = now

        got = int(min(want, b["tokens"]))
        b["tokens"] -= got
        return got

    def _peek_tokens(self, key: str) -> float:
        """看一眼还剩多少令牌（不扣减），仅用于提示文案。"""
        capacity = self._capacity()
        b = _BUCKETS.get(key)
        if b is None:
            return float(capacity)
        return min(
            float(capacity),
            b["tokens"] + (time.time() - b["last"]) / self._refill_seconds(),
        )

    def _junk_types(self) -> tuple:
        """按配置拼出要丢弃的类型元组。"""
        types: list = []
        if self.config.get("skip_voice", True):
            types.append(Record)
        if self.config.get("skip_video", True):
            types.append(Video)
        if self.config.get("skip_file", True):
            types.append(File)
        if self.config.get("skip_forward", True):
            types.extend((Node, Nodes))
        return tuple(types)

    # ------------------------------------------------------------------ 拦截
    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def pace(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True):
            return

        try:
            chain = list(event.message_obj.message or [])
        except Exception:
            return  # 拿不到消息链就不干预
        if not chain:
            return

        junk = self._junk_types()

        images: list = []
        text_parts: list[str] = []
        for seg in chain:
            if isinstance(seg, Image):
                images.append(seg)
            elif isinstance(seg, Plain):
                text_parts.append(seg.text or "")

        has_text = any(t.strip() for t in text_parts)

        # --- 1. 先剔除语音/视频/文件等 -----------------------------------
        kept = [seg for seg in chain if not isinstance(seg, junk)]
        dropped = len(kept) != len(chain)

        if dropped:
            if self.config.get("keep_text_alongside", True):
                # 保留文字（和 @、图片），只丢媒体本身
                if not has_text and not images:
                    # 整条消息只有被丢弃的内容 -> 静默忽略
                    event.stop_event()
                    event.should_call_llm(True)
                    logger.info(
                        f"[media_pacer] 跳过非文字消息 {self._who(event)}"
                        f"（类型 {self._types(chain)}）"
                    )
                    return
                self._rewrite(event, kept)
            else:
                # 不保留文字：只要含被丢弃类型就整条忽略
                if not images:
                    event.stop_event()
                    event.should_call_llm(True)
                    logger.info(
                        f"[media_pacer] 跳过含媒体消息 {self._who(event)}"
                        f"（类型 {self._types(chain)}）"
                    )
                    return
                self._rewrite(event, [s for s in kept if not isinstance(s, Plain)])

        # --- 2. 图片令牌桶 ----------------------------------------------
        if not images:
            return  # 纯文字，零干预

        bypass = False
        if self.config.get("bypass_admin", True):
            try:
                bypass = bool(event.is_admin())
            except Exception:
                bypass = False

        if bypass:
            logger.info(f"[media_pacer] 管理员图片放行 {len(images)} 张")
            return

        key = (
            "*"
            if self.config.get("per", "session") == "global"
            else (event.unified_msg_origin or "*")
        )
        got = self._take_tokens(key, len(images))

        if got >= len(images):
            logger.info(
                f"[media_pacer] 识别图片 {len(images)} 张"
                f"（余 {self._peek_tokens(key):.1f} 令牌）{self._who(event)}"
            )
            return

        # 超限
        note = self._exceeded_note(key)

        if self.config.get("on_exceeded", "skip") == "block":
            event.stop_event()
            event.should_call_llm(True)
            logger.info(
                f"[media_pacer] 图片超限，整条拦截 {self._who(event)}"
            )
            if note:
                await event.send(event.plain_result(note))
            return

        # skip：只移除超出的图片，文字照常
        allowed = set(id(x) for x in images[:got])
        new_chain = [
            seg
            for seg in (kept if dropped else chain)
            if (not isinstance(seg, Image)) or id(seg) in allowed
        ]
        self._rewrite(event, new_chain)
        logger.info(
            f"[media_pacer] 图片超限，识别 {got}/{len(images)} 张，"
            f"保留文字 {self._who(event)}"
        )
        if note:
            await event.send(event.plain_result(note))

    # ------------------------------------------------------------------ 辅助
    @staticmethod
    def _rewrite(event: AstrMessageEvent, new_chain: list):
        try:
            event.message_obj.message = new_chain
        except Exception as e:
            # 降级为不节流，而不是让消息处理失败
            logger.warning(f"[media_pacer] 改写消息链失败，本条不节流：{e}")

    @staticmethod
    def _types(chain) -> str:
        return ",".join(type(s).__name__ for s in chain) or "空"

    @staticmethod
    def _who(event: AstrMessageEvent) -> str:
        try:
            gid = event.get_group_id()
            sid = event.get_sender_id()
            return f"group:{gid} user:{sid}" if gid else f"private:{sid}"
        except Exception:
            return "unknown"

    def _exceeded_note(self, key: str) -> str:
        tpl = str(self.config.get("notify_on_exceeded", "") or "").strip()
        if not tpl:
            return ""
        refill = self._refill_seconds()
        # 距离补满 1 个令牌还差多少秒
        have = self._peek_tokens(key)
        left = max(1, int(round((1.0 - min(have, 1.0)) * refill)))
        return tpl.replace("{seconds}", str(left))
