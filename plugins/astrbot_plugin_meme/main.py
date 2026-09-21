"""
astrbot_plugin_meme -- 表情包

让模型按语境发表情包，并支持从群聊自动收集表情。

两种"表情"分开处理
------------------
* **Emoji 字符**（😂🐳）不属于本插件 —— 那只是模型输出的文本，靠人设鼓励即可，
  零代码、零成本。
* **表情包图片** 才是本插件负责的：模型没法直接发图，它只能输出文字。

模型怎么"要"一张图
------------------
模型输出标记：

    [表情:开心]

本插件在 `on_decorating_result` 钩子里把标记从文本中摘掉，换成图库里的图片。
选这个方案而不是 function calling，是因为它**只依赖文本生成能力** ——
任何模型都能用，不受 function calling 支持情况的影响。

标签用**固定词汇表**（在 _conf_schema 与 TAGS 里定义），模型不用描述图，
只需要挑一个情绪词。这比让它自由描述可靠得多。

收集
----
开启后会把群里发的表情包存进"待筛选池"。**收集不等于能用** ——
必须打标签之后才进图库、才能被模型选中。这样避免无关图片污染图库，
也避免自动打标签产生的 LLM 开销。

数据落在 data/plugin_data/ 下（不是插件目录），这样更新/重装插件不会丢数据。
"""

import asyncio
import contextlib
import hashlib
import json
import random
import re
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

try:  # StarTools 在旧版 AstrBot 上可能不存在
    from astrbot.api.star import StarTools
except Exception:  # pragma: no cover
    StarTools = None

# 固定标签词汇表。模型只能从这些里挑，避免自由发挥导致匹配不到图。
TAGS = [
    "开心", "大笑", "害羞", "无语", "疑问", "震惊",
    "生气", "委屈", "赞同", "拒绝", "累了", "吃饭",
    "哭泣", "得意", "摸鱼", "睡觉",
]

# 匹配 [表情:开心] / 【表情：开心】 等写法
MARKER_RE = re.compile(r"[\[【]\s*表情\s*[:：]\s*([^\]】\n]{1,12})\s*[\]】]")

# 自动打标签时要求模型输出的 JSON 形状
TAG_SYSTEM_PROMPT = (
    "你是一个表情包分类器。看图片，从给定标签里选**一个**最合适的。\n"
    "只能从这些标签里选：{tags}\n"
    "如果这张图不是表情包、或者看不出明确情绪，tag 输出空字符串。\n"
    '只输出 JSON，不要任何解释，格式：{{"tag": "标签"}}'
)

# 库索引（内存态，启动时从磁盘读一次）
_LIB: dict[str, list[str]] = {}
_POOL: list[str] = []
_LOADED = False

# 自动打标签：待处理队列 + 每日计数 + 后台任务句柄
_TAG_QUEUE: asyncio.Queue | None = None
_TAG_TASK: asyncio.Task | None = None
_TAG_DAY = ""
_TAG_COUNT = 0


class Meme(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    # ------------------------------------------------------------------ 路径
    #: 插件名。显式传给 StarTools.get_data_dir()，**不用它的调用栈推断** ——
    #: 那个推断依赖「调用者模块能在 star_map 里找到」，从辅助方法里调用时可能失败，
    #: 而失败会直接抛 RuntimeError。
    PLUGIN_NAME = "astrbot_plugin_meme"

    def _data_dir(self) -> Path:
        """插件数据目录。放这里而不是插件目录，更新或重装插件不会丢图库。"""
        base = None
        if StarTools is not None:
            try:
                base = Path(StarTools.get_data_dir(self.PLUGIN_NAME))
            except Exception as e:
                logger.warning(f"[meme] StarTools.get_data_dir 失败，改用兜底路径：{e}")
                base = None
        if base is None:
            # 兜底：AstrBot 数据目录下的 plugin_data（容器内即 /AstrBot/data）
            base = Path("/AstrBot/data/plugin_data") / self.PLUGIN_NAME
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"[meme] 创建数据目录失败：{e}")
        return base

    def _lib_dir(self) -> Path:
        d = self._data_dir() / "library"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _pool_dir(self) -> Path:
        d = self._data_dir() / "pool"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _index_path(self) -> Path:
        return self._data_dir() / "index.json"

    # ------------------------------------------------------------------ 索引
    def _load_index(self):
        global _LOADED
        if _LOADED:
            return
        _LOADED = True
        p = self._index_path()
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            lib = data.get("library") or {}
            if isinstance(lib, dict):
                for k, v in lib.items():
                    if isinstance(v, list):
                        _LIB[k] = [str(x) for x in v]
            pool = data.get("pool") or []
            if isinstance(pool, list):
                _POOL.extend(str(x) for x in pool)
        except Exception as e:
            logger.warning(f"[meme] 读取索引失败：{e}")

    def _save_index(self):
        try:
            self._index_path().write_text(
                json.dumps(
                    {"library": _LIB, "pool": _POOL},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[meme] 写入索引失败：{e}")

    def _count_library(self) -> int:
        return sum(len(v) for v in _LIB.values())

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self):
        global _TAG_QUEUE, _TAG_TASK
        self._load_index()
        logger.info(
            f"[meme] 启动：图库 {self._count_library()}/"
            f"{self.config.get('library_limit', 300)} 张，"
            f"待筛选 {len(_POOL)} 张，标签 {len(TAGS)} 个"
            + ("（收集已开）" if self.config.get("collect_enabled", True) else "")
        )
        if self.config.get("auto_tag", True):
            _TAG_QUEUE = asyncio.Queue()
            # 把池子里已有的图补进队列 —— 否则重启前收的图永远不会被打标签
            for p in list(_POOL):
                with contextlib.suppress(Exception):
                    _TAG_QUEUE.put_nowait(p)
            _TAG_TASK = asyncio.create_task(self._tag_worker())
            logger.info(
                f"[meme] 自动打标签已开：模型="
                f"{self.config.get('auto_tag_provider') or '当前对话模型'}，"
                f"每日上限 {self.config.get('auto_tag_daily_limit', 50)} 张"
                + (f"，补入 {_TAG_QUEUE.qsize()} 张待处理" if _TAG_QUEUE.qsize() else "")
            )

    async def terminate(self):
        global _TAG_TASK
        if _TAG_TASK:
            _TAG_TASK.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await _TAG_TASK
            _TAG_TASK = None
        logger.info("[meme] 已停止")

    def reload_index(self):
        """
        丢弃内存缓存，从磁盘重新读索引。

        用途：你手工往 library/ 目录放了图、或直接改了 index.json 之后，
        不重启插件也能让改动生效（配合 /表情 状态 触发）。
        """
        global _LOADED
        _LIB.clear()
        _POOL.clear()
        _LOADED = False
        self._load_index()

    # ------------------------------------------------------------------ 自动打标签
    async def _tag_worker(self):
        """
        后台打标签循环。

        为什么用队列 + 串行 + 间隔：2GB 内存 + 限速 API，并发调视觉模型
        很容易把内存打爆或者触发限流。串行慢一点，但稳。
        """
        global _TAG_DAY, _TAG_COUNT
        assert _TAG_QUEUE is not None
        interval = max(1, int(self.config.get("auto_tag_interval_seconds", 3) or 3))
        while True:
            try:
                path = await _TAG_QUEUE.get()
            except asyncio.CancelledError:
                raise
            try:
                # 跨天重置计数
                today = datetime.now().strftime("%Y-%m-%d")
                if _TAG_DAY != today:
                    _TAG_DAY = today
                    _TAG_COUNT = 0

                limit = int(self.config.get("auto_tag_daily_limit", 50) or 0)
                if limit > 0 and _TAG_COUNT >= limit:
                    # 超上限：留在池子里，明天/手动处理
                    if self.config.get("debug", False):
                        logger.info(
                            f"[meme] 今日自动打标签已达上限 {limit}，"
                            f"剩余图留在池子里"
                        )
                    # 把队列里剩下的也放回（避免死循环消费）
                    await asyncio.sleep(60)
                    continue

                # 图库满就不再打标签（否则打完也放不进去）
                try:
                    lib_limit = int(self.config.get("library_limit", 300) or 300)
                except Exception:
                    lib_limit = 300
                if lib_limit > 0 and self._count_library() >= lib_limit:
                    await asyncio.sleep(60)
                    continue

                if not Path(path).exists():
                    continue

                tag = await self._classify(path)
                _TAG_COUNT += 1

                if not tag:
                    mode = str(self.config.get("auto_tag_unclear", "keep"))
                    if mode == "discard":
                        with contextlib.suppress(Exception):
                            Path(path).unlink(missing_ok=True)
                        with contextlib.suppress(Exception):
                            _POOL.remove(path)
                        self._save_index()
                        logger.info("[meme] 判断不出情绪，已丢弃")
                    else:
                        logger.info("[meme] 判断不出情绪，留在池子里等手动处理")
                    continue

                self._move_to_library(path, tag)
                logger.info(
                    f"[meme] 自动打标签 [{tag}] -> {Path(path).name}"
                    f"（今日 {_TAG_COUNT}/{limit if limit > 0 else '∞'}）"
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[meme] 自动打标签出错")
            finally:
                with contextlib.suppress(Exception):
                    _TAG_QUEUE.task_done()
            await asyncio.sleep(interval)

    async def _get_provider(self):
        """
        取一个可用的对话模型。多级兜底。

        为什么不能只用 get_using_provider()：它**依赖会话**（umo）解析用户选择的
        模型，而自动打标签跑在后台任务里、没有会话 —— 拿不到。它也已标记 deprecated。

        顺序：
          ① 插件配置里指定的提供商 ID
          ② 配置里的 default_provider_id（AstrBot 的全局默认）
          ③ get_using_provider_async() —— 无会话时的解析结果
          ④ 任意一个可用的聊天模型
        """
        # ① 显式配置
        pid = str(self.config.get("auto_tag_provider", "") or "").strip()
        if pid:
            with contextlib.suppress(Exception):
                p = self.context.get_provider_by_id(pid)
                if p is not None:
                    return p
            logger.warning(f"[meme] 配置的提供商 {pid} 找不到，继续兜底")

        # ② AstrBot 全局默认
        with contextlib.suppress(Exception):
            cfg = self.context.get_config()
            dpid = str(cfg.get("default_provider_id", "") or "").strip()
            if dpid:
                p = self.context.get_provider_by_id(dpid)
                if p is not None:
                    return p

        # ③ 异步解析（不依赖会话）
        with contextlib.suppress(Exception):
            p = await self.context.get_using_provider_async()
            if p is not None:
                return p

        # ④ 任意一个
        with contextlib.suppress(Exception):
            allp = self.context.get_all_providers() or []
            if allp:
                return allp[0]

        return None

    async def _classify(self, path: str) -> str:
        """
        让视觉模型判断图片属于哪个标签。返回标签，判断不出返回空串。

        用 JSON 输出而不是自由文本：模型有时候会啰嗦（"这张图看起来很开心"），
        直接解析文本很脆。JSON 里取字段可靠得多。
        """
        provider = await self._get_provider()
        if provider is None:
            logger.warning(
                "[meme] 找不到可用的对话模型，自动打标签跳过。"
                "请确认 AstrBot 里配好了模型提供商，或在本插件配置里"
                "手动指定「打标签用的模型提供商 ID」。"
            )
            return ""

        prompt = TAG_SYSTEM_PROMPT.format(tags="、".join(TAGS))
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    image_urls=[path],
                    system_prompt="你是表情包分类器，只输出 JSON。",
                ),
                timeout=60,
            )
        except asyncio.TimeoutError:
            logger.warning("[meme] 打标签超时（60s），跳到下一张")
            return ""
        except Exception as e:
            logger.warning(f"[meme] 打标签请求失败：{e}")
            return ""

        text = ""
        with contextlib.suppress(Exception):
            text = (getattr(resp, "completion_text", "") or "").strip()
        if not text:
            return ""

        # 从回复里抠出 JSON（模型可能加了代码块或前后缀）
        data = None
        m = re.search(r"\{.*?\}", text, re.DOTALL)
        if m:
            with contextlib.suppress(Exception):
                data = json.loads(m.group(0))
        if not isinstance(data, dict):
            with contextlib.suppress(Exception):
                data = json.loads(text)
        if not isinstance(data, dict):
            if self.config.get("debug", False):
                logger.info(f"[meme] 打标签返回无法解析：{text[:80]}")
            return ""

        tag = str(data.get("tag") or "").strip()
        if tag and tag not in TAGS:
            # 模型偶尔会自己造词，兜一下：尝试在返回里找已知标签
            for t in TAGS:
                if t in tag:
                    return t
            if self.config.get("debug", False):
                logger.info(f"[meme] 模型给了词表外的标签：{tag}")
            return ""
        return tag

    def _move_to_library(self, path: str, tag: str) -> bool:
        """把一张图从池子移进图库。文件删掉+重建路径，避免跨文件系统 rename 失败。"""
        src = Path(path)
        try:
            lib_limit = int(self.config.get("library_limit", 300) or 300)
        except Exception:
            lib_limit = 300
        if lib_limit > 0 and self._count_library() >= lib_limit:
            return False

        dest = self._lib_dir() / src.name
        try:
            if dest.exists():
                dest = self._lib_dir() / f"{tag}_{src.name}"
            if src.exists():
                dest.write_bytes(src.read_bytes())
                src.unlink(missing_ok=True)
            elif not dest.exists():
                return False
        except Exception as e:
            logger.warning(f"[meme] 入库移动失败：{e}")
            return False

        with contextlib.suppress(Exception):
            _POOL.remove(path)
        _LIB.setdefault(tag, []).append(str(dest))
        self._save_index()
        return True

    # ------------------------------------------------------------------ 告诉模型有哪些标签
    @filter.on_llm_request()
    async def tell_tags(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.config.get("enabled", True):
            return
        if not self.config.get("send_enabled", True):
            return

        # 只列出**确实有图**的标签，否则模型会挑到空标签然后发不出图
        available = [t for t in TAGS if _LIB.get(t)]
        if not available:
            return

        hint = (
            "\n\n【表情包】\n"
            "你可以在回复的最后加一个表情标记，比如 [表情:开心]，"
            "系统会自动换成一张表情包图片。\n"
            "只能在合适的时候用，不要每条都加，也不要加两个以上。\n"
            f"可用的标记只有这些：{'、'.join(available)}。\n"
            "标记要写在回复的最末尾，不要在标记后面再写别的字。"
        )
        with contextlib.suppress(Exception):
            req.system_prompt = (req.system_prompt or "") + hint

    # ------------------------------------------------------------------ 发送
    def _pick_and_send(self, event: AstrMessageEvent, tag: str) -> bool:
        """
        从标签里随机挑一张图发出去（作为独立消息）。

        用 context.send_message 而不是往回复链里插图 —— 后者在流式输出下不可靠
        （内容已经边生成边发出去了，再去改链子来不及）。
        独立消息在任何输出模式下都成立。

        返回 True 表示真的发了。
        """
        files = _LIB.get(tag)
        if not files:
            return False
        path = random.choice(files)
        try:
            chain = MessageChain([Image.fromFileSystem(path)])
        except Exception as e:
            logger.warning(f"[meme] 构造图片失败：{e}")
            return False
        try:
            asyncio.create_task(self._send_later(event, chain, path))
        except Exception as e:
            # 没有事件循环时（理论上不该发生）退化为同步等待
            logger.warning(f"[meme] 排入发送任务失败：{e}")
            return False
        return True

    async def _send_later(self, event: AstrMessageEvent, chain, path: str):
        # 稍微延迟，让主回复先落地，避免图片跑到文字前面
        await asyncio.sleep(0.6)
        try:
            await self.context.send_message(event.unified_msg_origin, chain)
            logger.info(f"[meme] 发出表情 -> {Path(path).name}")
        except Exception as e:
            logger.warning(f"[meme] 发送表情失败：{e}")

    # ------------------------------------------------------------------ 替换标记为图片
    @filter.on_decorating_result()
    async def decorate(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True):
            return
        if not self.config.get("send_enabled", True):
            return

        # 双保险：本回合已经处理过就不再处理。
        # 防止钩子被重复调用时插两张图。
        with contextlib.suppress(Exception):
            if event.get_extra("_meme_sent"):
                return

        try:
            result = event.get_result()
        except Exception:
            return
        if result is None or not getattr(result, "chain", None):
            return

        # 流式输出下**不剥标记** —— 文本早就边生成边发出去了，这时候改链子没用。
        # 标记会留在已发出的文本里，由 after_message_sent 兜底补一张图。
        is_stream = self._is_streaming(result)

        found: list[str] = []
        new_chain = []
        for seg in result.chain:
            if isinstance(seg, Plain) and seg.text:
                def _sub(m):
                    tag = (m.group(1) or "").strip()
                    found.append(tag)
                    return ""
                text = MARKER_RE.sub(_sub, seg.text).strip()
                if text:
                    new_chain.append(Plain(text))
                continue
            new_chain.append(seg)

        if found:
            # 记下标记，供流式路径兜底使用
            try:
                event.set_extra("_meme_tags", found)
            except Exception:
                pass

        if not found:
            return

        if not is_stream:
            # 非流式：安全地把标记摘掉
            with contextlib.suppress(Exception):
                result.chain = new_chain

        tag = self._choose_tag(found, event)
        if not tag:
            return

        if not is_stream:
            # 非流式：主回复还没发出去，可以直接把图插在文字后面
            if self.config.get("require_text", True):
                has_text = any(
                    isinstance(s, Plain) and (s.text or "").strip()
                    for s in new_chain
                )
                if not has_text:
                    # 只有一张图没有文字，放弃；同时清标记避免兜底又发一次
                    self._mark_sent(event)
                    return
            try:
                new_chain.append(Image.fromFileSystem(random.choice(_LIB[tag])))
                with contextlib.suppress(Exception):
                    result.chain = new_chain
                # 关键：必须标记"已发过"，否则 after_sent 会再补一张
                self._mark_sent(event)
                logger.info(
                    f"[meme] 随回复附上表情 [{tag}] "
                    f"-> {Path(_LIB[tag][0]).name}"
                )
            except Exception as e:
                logger.warning(f"[meme] 附图失败，改用独立消息：{e}")
                if self._pick_and_send(event, tag):
                    self._mark_sent(event)
            return

        # 流式：只能发独立消息
        if self._pick_and_send(event, tag):
            self._mark_sent(event)
            if self.config.get("debug", False):
                logger.info(f"[meme] 流式模式：表情 [{tag}] 以独立消息发送")
        return

    @staticmethod
    def _mark_sent(event: AstrMessageEvent):
        """
        标记本回合已经发过表情，让 after_sent 的兜底不再重复发。

        为什么需要：decorate 和 after_sent 是两个独立钩子。
        非流式路径下 decorate 已经把图插进回复链了，如果不清掉标记，
        after_sent 看到标记还在就会再补一张 ——
        表现就是「一次回复发了两张表情包」。
        """
        with contextlib.suppress(Exception):
            event.set_extra("_meme_tags", None)
            event.set_extra("_meme_sent", True)

    @staticmethod
    def _is_streaming(result) -> bool:
        """判断这次结果是不是流式输出。取不到信息时按非流式处理。"""
        try:
            from astrbot.core.message.message_event_result import ResultContentType
        except Exception:
            return False
        try:
            rct = getattr(result, "result_content_type", None)
            if rct is None:
                return False
            return rct in (
                ResultContentType.STREAMING_RESULT,
                ResultContentType.STREAMING_FINISH,
            )
        except Exception:
            return False

    def _choose_tag(self, found: list[str], event: AstrMessageEvent) -> str:
        """从模型给的标记里挑一个库里有图的标签。同时做概率过滤。"""
        tag = ""
        for t in found:
            if _LIB.get(t):
                tag = t
                break
        if not tag:
            logger.info(f"[meme] 模型用了无效标签 {found}，已忽略")
            return ""
        try:
            prob = float(self.config.get("send_probability", 0.7))
        except Exception:
            prob = 0.7
        if prob < 1.0 and random.random() > prob:
            if self.config.get("debug", False):
                logger.info(f"[meme] 标签 {tag} 概率未过，跳过发图")
            return ""
        return tag

    # ------------------------------------------------------------------ 流式兜底
    @filter.after_message_sent()
    async def after_sent(self, event: AstrMessageEvent):
        """
        只在**流式路径**下补发表情。

        流式模式下 on_decorating_result 拿不到完整文本（内容已边生成边发出），
        所以那里发不出图。这里检查有没有遗留的标记，有就补发一张独立图片。

        非流式路径不会走到这里 —— decorate 插完图会调 _mark_sent() 清掉标记。
        这道判断是双保险：万一钩子被重复调用，也不会发两张。

        副作用（仅流式）：标记本身已随文本发出，用户会看到 [表情:开心]。
        这是流式的固有限制 —— 想彻底避免，请关闭流式输出。
        """
        # 双保险：非流式路径已经发过了就跳过
        already = False
        with contextlib.suppress(Exception):
            already = bool(event.get_extra("_meme_sent"))
        if already:
            return

        tags = None
        with contextlib.suppress(Exception):
            tags = event.get_extra("_meme_tags")
        if not tags:
            return
        # 先清标记，避免并发/重复触发时发两次
        self._mark_sent(event)

        if not self.config.get("send_enabled", True):
            return
        tag = self._choose_tag(list(tags), event)
        if not tag:
            return
        if self._pick_and_send(event, tag):
            logger.info(
                f"[meme] 流式兜底补发表情 [{tag}]。"
                f"想避免标记外露，请在 AstrBot 关闭流式输出。"
            )

    # ------------------------------------------------------------------ 收集
    @filter.event_message_type(filter.EventMessageType.ALL, priority=900)
    async def collect(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True):
            return
        if not self.config.get("collect_enabled", True):
            return

        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            group_id = ""
        if not group_id:
            return  # 只从群里收

        allow = [str(x).strip() for x in (self.config.get("collect_groups") or [])]
        if allow and group_id not in allow:
            return

        # 池子满了就不再收，避免无限膨胀
        try:
            pool_limit = int(self.config.get("pool_limit", 60) or 60)
        except Exception:
            pool_limit = 60
        if pool_limit > 0 and len(_POOL) >= pool_limit:
            return

        try:
            chain = list(event.message_obj.message or [])
        except Exception:
            return

        for seg in chain:
            if not isinstance(seg, Image):
                continue
            if not self._looks_like_sticker(event, seg):
                continue
            try:
                await self._save_to_pool(seg)
            except Exception as e:
                if self.config.get("debug", False):
                    logger.info(f"[meme] 收集失败：{e}")
            return  # 一条消息只收一张

    @staticmethod
    def _looks_like_sticker(event: AstrMessageEvent, seg) -> bool:
        """
        判断是不是表情包。

        优先看 OneBot 原始消息里的 sub_type：NapCat 对表情包会标成 "1"，
        普通图片是 "0"。这是最可靠的信号。

        拿不到 sub_type 时（非 OneBot 平台、或结构不同）**宽松放行** ——
        宁可多收几张让你筛选，也不要漏掉。反正都要人工打标签。
        """
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return True
        msg = raw.get("message")
        if not isinstance(msg, list):
            return True
        for item in msg:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "image":
                continue
            data = item.get("data")
            if not isinstance(data, dict):
                continue
            sub = str(data.get("sub_type", "")).strip()
            if sub == "1":
                return True
            if sub == "0":
                return False
            return True  # 有 image 段但没 sub_type，宽松放行
        return True

    async def _save_to_pool(self, seg):
        # 大小限制（3Mbps 带宽下这条很重要）
        try:
            max_mb = float(self.config.get("max_file_mb", 1.0) or 1.0)
        except Exception:
            max_mb = 1.0

        path = await seg.convert_to_file_path()
        if not path:
            return
        src = Path(path)
        if not src.is_file():
            return
        if max_mb > 0 and src.stat().st_size > max_mb * 1024 * 1024:
            if self.config.get("debug", False):
                logger.info(f"[meme] 图片超过 {max_mb}MB，跳过收集")
            return

        # 用内容哈希去重：库里和池子里已有的都不再收
        try:
            digest = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
        except Exception:
            return
        name = f"{digest}{src.suffix.lower() or '.jpg'}"

        for existing in _POOL:
            if Path(existing).name.startswith(digest):
                return
        for files in _LIB.values():
            for existing in files:
                if Path(existing).name.startswith(digest):
                    return

        dest = self._pool_dir() / name
        if dest.exists():
            return
        try:
            dest.write_bytes(src.read_bytes())
        except Exception as e:
            logger.warning(f"[meme] 写入待筛选池失败：{e}")
            return

        _POOL.append(str(dest))
        self._save_index()
        logger.info(
            f"[meme] 收集到表情，待筛选池 {len(_POOL)} 张"
            + (
                "（已排队自动打标签）"
                if (self.config.get("auto_tag", True) and _TAG_QUEUE is not None)
                else "（需要手动打标签才能被模型使用）"
            )
        )

        # 排进自动打标签队列
        if self.config.get("auto_tag", True) and _TAG_QUEUE is not None:
            with contextlib.suppress(Exception):
                _TAG_QUEUE.put_nowait(str(dest))

    # ------------------------------------------------------------------ 管理指令
    def _is_admin(self, event: AstrMessageEvent) -> bool:
        cfg = str(self.config.get("admin_qq", "") or "").strip()
        with contextlib.suppress(Exception):
            if cfg and str(event.get_sender_id()) == cfg:
                return True
        with contextlib.suppress(Exception):
            return bool(event.is_admin())
        return False

    @filter.command("表情")
    async def meme_cmd(self, event: AstrMessageEvent, sub: str = "", arg: str = ""):
        """
        表情包管理。用法：
        /表情 状态
        /表情 池        —— 列出待筛选池
        /表情 标签 <编号> <标签>   —— 给池里的图打标签并入库
        /表情 丢弃 <编号>
        /表情 标签表
        """
        if not self._is_admin(event):
            yield event.plain_result("这个指令只有管理员能用。")
            return

        sub = (sub or "").strip()
        arg = (arg or "").strip()

        if sub in ("", "状态"):
            tag_state = "关闭"
            if self.config.get("auto_tag", True):
                try:
                    limit = int(self.config.get("auto_tag_daily_limit", 50) or 0)
                except Exception:
                    limit = 50
                tag_state = f"开（今日 {_TAG_COUNT}/{limit if limit > 0 else '∞'}）"
                if _TAG_QUEUE is not None:
                    tag_state += f"，队列 {_TAG_QUEUE.qsize()} 张待处理"
            yield event.plain_result(
                f"图库 {self._count_library()}/{self.config.get('library_limit', 300)} 张\n"
                f"待筛选池 {len(_POOL)} 张\n"
                f"可用标签 {len([t for t in TAGS if _LIB.get(t)])}/{len(TAGS)}\n"
                f"自动打标签：{tag_state}"
            )
            return

        if sub in ("标签表", "tags"):
            lines = [f"{t}：{len(_LIB.get(t, []))} 张" for t in TAGS]
            yield event.plain_result("标签库存：\n" + "\n".join(lines))
            return

        if sub in ("池", "pool"):
            if not _POOL:
                yield event.plain_result("待筛选池是空的。")
                return
            lines = [f"{i+1}. {Path(p).name}" for i, p in enumerate(_POOL[:30])]
            more = "" if len(_POOL) <= 30 else f"\n...还有 {len(_POOL)-30} 张"
            yield event.plain_result(
                "待筛选池：\n" + "\n".join(lines) + more
                + "\n\n用 /表情 标签 <编号> <标签> 入库"
            )
            return

        if sub in ("丢弃", "drop"):
            try:
                idx = int(arg) - 1
                p = _POOL.pop(idx)
            except Exception:
                yield event.plain_result("用法：/表情 丢弃 <编号>")
                return
            with contextlib.suppress(Exception):
                Path(p).unlink(missing_ok=True)
            self._save_index()
            yield event.plain_result(f"已丢弃，剩余 {len(_POOL)} 张。")
            return

        if sub in ("标签", "tag"):
            # arg 形如 "3 开心"
            parts = arg.split()
            if len(parts) != 2:
                yield event.plain_result("用法：/表情 标签 <编号> <标签>")
                return
            try:
                idx = int(parts[0]) - 1
                tag = parts[1].strip()
            except Exception:
                yield event.plain_result("用法：/表情 标签 <编号> <标签>")
                return
            if tag not in TAGS:
                yield event.plain_result(
                    f"标签必须是这些之一：{'、'.join(TAGS)}"
                )
                return
            try:
                limit = int(self.config.get("library_limit", 300) or 300)
            except Exception:
                limit = 300
            if limit > 0 and self._count_library() >= limit:
                yield event.plain_result(
                    f"图库已满（{limit} 张）。先删掉一些，或调高上限。"
                )
                return
            try:
                src = Path(_POOL.pop(idx))
            except Exception:
                yield event.plain_result("编号不对。用 /表情 池 看当前列表。")
                return

            dest = self._lib_dir() / src.name
            try:
                if src.exists():
                    src.replace(dest)
                else:
                    yield event.plain_result("源文件不见了，已跳过。")
                    return
            except Exception as e:
                yield event.plain_result(f"移动文件失败：{e}")
                return

            _LIB.setdefault(tag, []).append(str(dest))
            self._save_index()
            yield event.plain_result(
                f"已入库 [{tag}]，图库 {self._count_library()}/{limit} 张。"
            )
            return

        yield event.plain_result(
            "用法：\n"
            "/表情 状态\n"
            "/表情 池\n"
            "/表情 标签 <编号> <标签>\n"
            "/表情 丢弃 <编号>\n"
            "/表情 标签表"
        )
