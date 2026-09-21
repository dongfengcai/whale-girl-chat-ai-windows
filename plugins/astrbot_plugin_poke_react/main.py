"""
astrbot_plugin_poke_react -- 戳一戳回应

需求：让机器人对「戳一戳」有反应，并且**把戳一戳视为与 @ 一致**
（参与需求 7 的多人仲裁）。

设计要点
--------
1. **必须在 media_pacer 之前运行。** 那个插件把 Poke 当成"其它类型"从消息链里
   移除；一旦被移除，这里就再也看不到戳一戳了。所以本插件用 priority=1100，
   比 media_pacer 的 1000 更早。

2. **注入 At 组件来实现"与 @ 一致"。** 不在 reply_arbiter 里加特判，而是把
   戳一戳转换成结构上真正的 @：这样 reply_arbiter 现有的 `_mentioned()`
   检测、优先级判定、合并窗口全部原样生效，不需要改它一行。
   这也符合事实 —— 戳一戳本来就是"点名找某人"。

3. **必须附带文字。** 只有 At 而没有 Plain 的话，media_pacer 会认为"没有实质
   内容"而整条拦掉；而且模型看到一条纯 @ 也不知道该回什么。所以注入一句
   描述（可配置），模型再按人设去演绎反应。

组件结构（依据 astrbot/core/message/components.py）：

    class Poke(BaseMessageComponent):
        _type: str | int = "126"          # 126 = 戳一戳
        id: int | str | None = 0
        qq: int | str | None = 0          # 已废弃，兼容保留
        def target_id(self) -> str | None # 官方推荐的目标 ID 取法

    注意 `qq` 字段已标记 deprecated，所以优先用 `target_id()`，
    再退回 `qq` / `id` —— 兼容不同 AstrBot 版本。
"""

import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Poke, Plain
from astrbot.api.star import Context, Star

# 会话 -> 上次回应戳一戳的时间戳，用于冷却
_LAST_POKE: dict[str, float] = {}


def _poke_target(seg) -> str | None:
    """取戳一戳的目标 ID，兼容新旧字段。"""
    # 官方推荐：有 target_id() 就用它
    fn = getattr(seg, "target_id", None)
    if callable(fn):
        try:
            value = fn()
            if value:
                return str(value).strip()
        except Exception:
            pass
    # 回退：新字段 id，再回退废弃字段 qq
    for attr in ("id", "qq"):
        value = getattr(seg, attr, None)
        if value is None:
            continue
        text = str(value).strip()
        if text and text != "0":
            return text
    return None


class PokeReact(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    async def initialize(self):
        logger.info(
            "[poke_react] 启动：戳一戳"
            f"{'视为 @，参与仲裁' if self.config.get('inject_at', True) else '仅回应不参与仲裁'}"
            f"，冷却 {self.config.get('cooldown_seconds', 5)} 秒"
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1100)
    async def react(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True):
            return

        try:
            chain = list(event.message_obj.message or [])
        except Exception:
            return
        if not chain:
            return

        # 找出戳一戳的段（一条消息可能有多个，取第一个命中的）
        poke_seg = None
        for seg in chain:
            if isinstance(seg, Poke):
                poke_seg = seg
                break
        if poke_seg is None:
            return

        # 是否只响应戳自己
        if self.config.get("only_when_poked", True):
            try:
                me = str(event.get_self_id())
            except Exception:
                me = ""
            target = _poke_target(poke_seg)
            if not me or target != me:
                if self.config.get("debug", False):
                    logger.info(
                        f"[poke_react] 忽略戳向他人：target={target} self={me}"
                    )
                return

        # 冷却
        key = self._session_key(event)
        cooldown = max(0, int(self.config.get("cooldown_seconds", 5) or 0))
        if cooldown > 0:
            last = _LAST_POKE.get(key, 0.0)
            if (time.time() - last) < cooldown:
                if self.config.get("debug", False):
                    logger.info(f"[poke_react] {key} 冷却中，忽略")
                return
            _LAST_POKE[key] = time.time()

        # 改写消息链：把 Poke 换成「@自己 + 一句提示语」
        note = str(self.config.get("reaction_text", "有人戳了你一下") or "").strip()
        if not note:
            note = "有人戳了你一下"

        # 原有文字（如果有的话）。纯戳一戳时为 None。
        original_text = ""
        try:
            original_text = (event.message_str or "").strip()
        except Exception:
            original_text = ""

        new_chain = []
        for seg in chain:
            if isinstance(seg, Poke):
                # 注入 At，让 reply_arbiter 的 @ 检测原样生效
                if self.config.get("inject_at", True):
                    try:
                        new_chain.append(At(qq=event.get_self_id()))
                    except Exception as e:
                        logger.warning(f"[poke_react] 注入 At 失败：{e}")
                continue  # Poke 本身丢弃，避免被 media_pacer 当垃圾再处理一遍
            new_chain.append(seg)

        # 补一句文字：模型需要知道发生了什么，media_pacer 也需要"实质内容"
        if not any(
            isinstance(s, Plain) and (s.text or "").strip() for s in new_chain
        ):
            new_chain.append(Plain(note))

        try:
            event.message_obj.message = new_chain
        except Exception as e:
            logger.warning(f"[poke_react] 改写消息链失败：{e}")
            return

        # ------------------------------------------------------------------
        # 关键：群聊必须手动置唤醒标志
        #
        # WakingStage 的唤醒判定分两套（源码 astrbot/core/pipeline/waking_check/stage.py）：
        #
        #   私聊：无条件 is_at_or_wake_command = True
        #   群聊：扫描消息链，必须找到 At(自己) / AtAll / 引用自己
        #
        # 而 WakingStage 在本插件的 handler **之前**就执行完了 —— 那时戳一戳
        # 消息里还没有 At。所以：
        #
        #   * 私聊戳一戳能通，是因为私聊本来就无条件唤醒（跟注入 At 无关）
        #   * 群聊戳一戳不通，因为判定那一刻没看到 At，事后注入已经晚了
        #
        # 因此这里直接把标志位置上，不依赖它重新检测。
        # ProcessStage 的放行条件是：
        #     not _has_send_oper and is_at_or_wake_command and not call_llm
        # ------------------------------------------------------------------
        try:
            event.is_at_or_wake_command = True
            event.is_wake = True
        except Exception as e:
            logger.warning(f"[poke_react] 置唤醒标志失败，群聊可能不回复：{e}")

        # ------------------------------------------------------------------
        # 关键：必须同时改写 event.message_str
        #
        # AstrBot 构造 LLM 提示词时用的是它，而不是消息链
        # （astr_main_agent.py: req.prompt = event.message_str[...]）。
        # message_str 是消息进来时由原始消息算好的；戳一戳没有文字，
        # 所以它是空串 —— 只改消息链的话，模型收到的提示词是空的，不会回复。
        # ------------------------------------------------------------------
        try:
            if original_text:
                # 戳一戳夹带了文字，保留文字并追加旁白
                event.message_str = f"{original_text}（{note}）"
            else:
                event.message_str = note
        except Exception as e:
            logger.warning(f"[poke_react] 改写 message_str 失败，模型可能无回复：{e}")

        # 诊断：把决定「是否调用 LLM」的三个标志打出来。
        # ProcessStage 的判据是：
        #     not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm
        # 任何一个不满足，模型都不会被调用。
        def _flag(name, default="?"):
            try:
                return repr(getattr(event, name))
            except Exception as e:
                return f"<读取失败 {e}>"

        logger.info(
            f"[poke_react] 戳一戳 -> 触发回应 {self._who(event)}"
            f"（{'已注入 @' if self.config.get('inject_at', True) else '未注入 @'}"
            f"，提示词=\"{event.message_str}\"）"
        )
        logger.info(
            f"[poke_react] 诊断：is_at_or_wake_command={_flag('is_at_or_wake_command')}"
            f" is_wake={_flag('is_wake')}"
            f" call_llm={_flag('call_llm')}"
            f" _has_send_oper={_flag('_has_send_oper')}"
            f" is_stopped={_flag('is_stopped') if not callable(getattr(event, 'is_stopped', None)) else event.is_stopped()}"
        )

    # ------------------------------------------------------------------ 辅助
    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        try:
            return event.unified_msg_origin or "?"
        except Exception:
            return "?"

    @staticmethod
    def _who(event: AstrMessageEvent) -> str:
        try:
            gid = event.get_group_id()
            sid = event.get_sender_id()
            return f"group:{gid} user:{sid}" if gid else f"private:{sid}"
        except Exception:
            return "unknown"
