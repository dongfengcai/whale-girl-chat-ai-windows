"""
astrbot_plugin_balance_guard -- 余额红线守护

需求 1：检测 DeepSeek 余额，设置红线，触线后提示并停止回复。

行为
----
每 N 分钟轮询一次 GET https://api.deepseek.com/user/balance：

    余额 > 红线   -> 正常
    余额 <= 红线  -> ① 发一次告警  ② 熔断（拒绝后续消息进 LLM）

采用方案 B：熔断时不退出进程、不停止容器，只是把消息拦住。充值后可以用
管理指令立即解除，或者等下一次轮询自动发现余额回升后自动解除。

实现要点（对照 AstrBot 源码验证过的行为）
----------------------------------------
* 拦截用 `event.stop_event()`（跳过后续处理器）+ `event.should_call_llm(True)`。
  注意后者**名字是反的**：传 True 表示「不要调用 LLM」，这是源码里
  ProcessStage 的判据 `not event.call_llm`。
* 熔断判断必须发生在**每一条消息**上，而不是只在轮询回调里改状态 ——
  轮询间隔是分钟级，中间来的消息照样会把钱花掉。
* `priority=999` 让本插件排在其它处理器之前（源码里按 -priority 排序）。
"""

import asyncio
import contextlib
import time
from datetime import datetime, timedelta, timezone

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

BALANCE_URL = "https://api.deepseek.com/user/balance"
HTTP_TIMEOUT = 15  # 秒

# 群用量查询的缓存时长（秒）。每条消息都查一次数据库没必要，
# 30 秒的粒度对"每日额度"这种量级完全够用。
_USAGE_CACHE_TTL = 30


def _group_umo(event: AstrMessageEvent) -> str:
    """
    取群会话标识。

    `unified_msg_origin` 形如 `aiocqhttp:GroupMessage:123456`，
    而 `provider_stats.umo` 存的是同一个字段，所以可以直接作为查询条件。
    私聊返回空串（额度只按群统计）。
    """
    try:
        if event.is_private_chat():
            return ""
    except Exception:
        pass
    try:
        gid = event.get_group_id()
    except Exception:
        gid = ""
    if not gid:
        return ""
    umo = ""
    with contextlib.suppress(Exception):
        umo = event.unified_msg_origin or ""
    if umo:
        return umo
    with contextlib.suppress(Exception):
        return f"{event.get_platform_name()}:GroupMessage:{gid}"
    return ""

# 余额这一份缓存是所有实例共享的（轮询任务和消息拦截都要读），
# 用模块级变量而不是实例属性，避免每条消息都要走一次 async 读。
_CACHE: dict = {
    "ok": False,          # 是否拿到过有效余额
    "balance": None,      # 当前余额（配置币种口径）
    "currency": None,
    "field": None,
    "fused": False,       # 是否处于熔断
    "last_check": 0.0,    # 上次成功查询的时间戳
    "fail_streak": 0,     # 连续查询失败次数
    "notified": False,    # 本轮触线是否已告警
    "reason": "",         # 熔断原因，用于日志
}

# 群用量查询缓存：umo -> (时间戳, token 数)
_USAGE_CACHE: dict[str, tuple[float, int]] = {}

# 已经提示过"额度用尽"的群，避免每条消息都刷提示
_QUOTA_NOTIFIED: set[str] = set()

# 按字符估算的用量（表查询失败时的兜底）：umo -> (日期, token 估算值)
_ESTIMATE: dict[str, tuple[str, int]] = {}

# 估算用量的扣减基线（管理员重置额度时记录）
_ESTIMATE_CHECKPOINT: dict[str, int] = {}

# "已退化为估算"只提示一次，否则每条消息都刷日志
_ESTIMATE_WARNED = False


class BalanceGuard(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self):
        """插件加载后拉起轮询任务。"""
        # 熔断状态要跨重启保留：如果已经没钱了，重启后不该又开始烧钱。
        try:
            self._restore_fused = bool(await self.get_kv_data("fused", False))
            if self._restore_fused:
                _CACHE["fused"] = True
                _CACHE["reason"] = "重启前已熔断"
                logger.warning("[balance_guard] 检测到上次运行处于熔断状态，继续熔断")
        except Exception:
            # KV 不可用不该阻止插件工作，只是失去跨重启记忆
            logger.warning("[balance_guard] 读取熔断状态失败，按未熔断处理")

        interval = max(1, int(self.config.get("check_interval_minutes", 10)))
        logger.info(
            f"[balance_guard] 启动：红线 {self.config.get('red_line', 5.0)} "
            f"{self.config.get('currency', 'CNY')}，间隔 {interval} 分钟"
        )
        self._task = asyncio.create_task(self._poll_loop(interval))

    async def terminate(self):
        """插件卸载/重载时取消轮询任务。"""
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("[balance_guard] 已停止")

    # ------------------------------------------------------------------ 轮询
    async def _poll_loop(self, interval_minutes: int):
        # 启动后先立刻查一次，否则前十分钟是「无保护」状态
        await asyncio.sleep(3)
        while True:
            try:
                await self._check_and_act()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[balance_guard] 轮询出现未预期错误")
            await asyncio.sleep(interval_minutes * 60)

    async def _check_and_act(self):
        if not self.config.get("enabled", True):
            return

        balance = await self._fetch_balance()
        if balance is None:
            # 查询失败不熔断 —— 宁可漏报也不误杀
            _CACHE["fail_streak"] += 1
            streak = _CACHE["fail_streak"]
            threshold = max(1, int(self.config.get("failure_threshold", 3)))
            logger.warning(
                f"[balance_guard] 余额查询失败（连续 {streak}/{threshold} 次）"
            )
            if streak == threshold:
                await self._broadcast(
                    f"⚠️ 余额查询连续失败 {streak} 次，请检查网络或 API Key。\n"
                    f"机器人暂未熔断。"
                )
            return

        _CACHE["fail_streak"] = 0
        _CACHE.update(balance)
        bal, cur, field = balance["balance"], balance["currency"], balance["field"]
        red = float(self.config.get("red_line", 5.0))

        if bal <= red:
            if not _CACHE["fused"]:
                _CACHE["fused"] = True
                _CACHE["reason"] = f"余额 {bal:.2f} ≤ 红线 {red:.2f}"
                logger.warning(
                    f"[balance_guard] {_CACHE['reason']} -> 触发熔断"
                )
                with contextlib.suppress(Exception):
                    await self.put_kv_data("fused", True)
            if not _CACHE["notified"] or not self.config.get("notify_once", True):
                _CACHE["notified"] = True
                await self._broadcast(
                    "⚠️ **余额红线告警**\n"
                    f"当前余额 {cur} {bal:.2f}，已低于红线 {cur} {red:.2f}。\n"
                    f"机器人已暂停回复。充值后发送「"
                    f"{self.config.get('resume_command', '余额恢复')}」即可恢复。\n"
                    f"充值地址：https://platform.deepseek.com/top_up"
                )
                logger.info("[balance_guard] 已发送告警")
        else:
            if _CACHE["fused"]:
                _CACHE["fused"] = False
                _CACHE["reason"] = ""
                _CACHE["notified"] = False
                with contextlib.suppress(Exception):
                    await self.put_kv_data("fused", False)
                logger.info(f"[balance_guard] 余额 {bal:.2f} > 红线 -> 解除熔断")
                await self._broadcast(
                    f"✅ 余额已恢复（{cur} {bal:.2f}），机器人继续服务。"
                )
            else:
                logger.info(f"[balance_guard] 余额 {cur} {bal:.2f}（{field}）-> 正常")

    # ------------------------------------------------------------------ HTTP
    async def _resolve_api_key(self) -> str:
        """
        取 API Key：优先用插件自己的配置，留空则从 AstrBot 的模型提供商里找。

        这样用户通常不需要在插件里再填一遍 key。回退是尽力而为的 ——
        拿不到就返回空串，由调用方报错。
        """
        key = str(self.config.get("api_key", "") or "").strip()
        if key:
            return key

        # 遍历已加载的提供商，找 httpx/openai 客户端上的 api_key。
        # 不同提供商实现不一，所以整个循环都包在 try 里。
        try:
            providers = self.context.get_all_providers()
            if asyncio.iscoroutine(providers):
                providers = await providers
            for p in providers or []:
                for attr in ("api_key", "_api_key"):
                    v = getattr(p, attr, None)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
                    # 有的实现把 key 放在 client 里
                    client = getattr(p, "client", None)
                    v2 = getattr(client, attr, None) if client else None
                    if isinstance(v2, str) and v2.strip():
                        return v2.strip()
        except Exception as e:
            logger.warning(f"[balance_guard] 从提供商读取 API Key 失败：{e}")
        return ""

    async def _fetch_balance(self) -> dict | None:
        """查询余额。成功返回规范化字典，失败返回 None。"""
        key = await self._resolve_api_key()
        if not key:
            logger.error(
                "[balance_guard] 没有可用的 API Key。请在插件配置里填写，"
                "或确认 AstrBot 的模型提供商已配置。"
            )
            return None

        want_cur = str(self.config.get("currency", "CNY")).upper()
        field = str(self.config.get("balance_field", "total_balance"))

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
            ) as session:
                async with session.get(
                    BALANCE_URL,
                    headers={"Authorization": f"Bearer {key}"},
                ) as resp:
                    if resp.status == 401:
                        logger.error("[balance_guard] API Key 无效（401）")
                        return None
                    if resp.status != 200:
                        logger.warning(
                            f"[balance_guard] 余额接口返回 {resp.status}"
                        )
                        return None
                    data = await resp.json()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[balance_guard] 请求余额失败：{e}")
            return None

        # is_available=false 表示账户已无法调用 API，直接视同触线
        if data.get("is_available") is False:
            logger.warning("[balance_guard] 账户 is_available=false")
            return {
                "ok": True,
                "balance": 0.0,
                "currency": want_cur,
                "field": field,
                "last_check": time.time(),
            }

        infos = data.get("balance_infos") or []
        if not infos:
            logger.warning(f"[balance_guard] 余额响应没有 balance_infos：{data}")
            return None

        # 多币种时只取配置的那个
        chosen = None
        for info in infos:
            if str(info.get("currency", "")).upper() == want_cur:
                chosen = info
                break
        if chosen is None:
            chosen = infos[0]
            logger.warning(
                f"[balance_guard] 没有 {want_cur} 的余额记录，改用 "
                f"{chosen.get('currency')}"
            )

        raw = chosen.get(field)
        try:
            bal = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                f"[balance_guard] 字段 {field} 不是数字：{raw!r}（原始响应 {data}）"
            )
            return None

        return {
            "ok": True,
            "balance": bal,
            "currency": str(chosen.get("currency", want_cur)).upper(),
            "field": field,
            "last_check": time.time(),
        }

    # ------------------------------------------------------------------ 通知
    async def _targets(self) -> list[str]:
        """告警要发到哪些会话（umo 列表）。"""
        out: list[str] = []
        admin = str(self.config.get("admin_qq", "") or "").strip()
        if admin:
            # 私聊 UMO 形如 platform:private:QQ；平台名按实际适配器而定，
            # 这里给出常见值，失败时 send_message 会记日志。
            out.append(f"aiocqhttp:private:{admin}")
        for t in self.config.get("notify_targets", []) or []:
            t = str(t).strip()
            if not t:
                continue
            if t.startswith("group:"):
                out.append(f"aiocqhttp:group:{t.split(':', 1)[1]}")
            elif t.startswith("private:"):
                out.append(f"aiocqhttp:private:{t.split(':', 1)[1]}")
            else:
                out.append(t)  # 已经是完整 umo
        return out

    async def _broadcast(self, text: str):
        from astrbot.api.message_components import Plain
        from astrbot.api.event import MessageChain

        for umo in await self._targets():
            try:
                await self.context.send_message(umo, MessageChain([Plain(text)]))
            except Exception as e:
                logger.warning(f"[balance_guard] 发送告警到 {umo} 失败：{e}")

    # ------------------------------------------------------------------ 拦截
    @filter.event_message_type(filter.EventMessageType.ALL, priority=999)
    async def guard(self, event: AstrMessageEvent):
        """
        每一条消息都过这里。

        熔断中：拦住消息并回一句固定提示（可配置为空 = 完全静默）。
        正常时：什么都不做，直接放行给后续处理器和 LLM。
        """
        if not self.config.get("enabled", True):
            return

        text = event.message_str.strip()

        # --- 管理员「群额度恢复」指令（在任何熔断状态下都要能用）-------------
        gcmd = str(self.config.get("group_limit_command", "额度恢复") or "").strip()
        if gcmd and text.startswith(gcmd) and self._is_admin(event):
            event.stop_event()
            event.should_call_llm(True)
            target = text[len(gcmd):].strip()
            if not target:
                await event.send(event.plain_result("用法：<指令> <群号>"))
                return
            ok = await self._reset_group_usage(target)
            await event.send(event.plain_result(
                f"已重置群 {target} 今日额度。" if ok
                else f"重置失败，请查看日志（群 {target}）。"
            ))
            return

        # --- 管理员余额恢复指令 --------------------------------------------
        cmd = str(self.config.get("resume_command", "余额恢复") or "").strip()
        if cmd and text == cmd and self._is_admin(event):
            event.stop_event()
            event.should_call_llm(True)  # 名字是反的：True = 不调用 LLM
            await event.send(event.plain_result("正在重新查询余额…"))
            await self._check_and_act()
            bal = _CACHE["balance"]
            cur = _CACHE["currency"] or ""
            if _CACHE["fused"]:
                txt = (
                    f"仍然低于红线（当前 {cur} {bal:.2f}）。"
                    "请先充值：https://platform.deepseek.com/top_up"
                    if bal is not None
                    else "余额查询失败，请检查配置或稍后再试。"
                )
            else:
                txt = f"已恢复（当前 {cur} {bal:.2f}），继续服务。"
            await event.send(event.plain_result(txt))
            return

        # --- 全局熔断 -------------------------------------------------------
        if _CACHE["fused"]:
            event.stop_event()
            event.should_call_llm(True)
            blocked = str(self.config.get("blocked_reply", "") or "").strip()
            if blocked:
                await event.send(event.plain_result(blocked))
            return

        # --- 每群每日 token 额度 --------------------------------------------
        await self._check_group_quota(event)

    # ------------------------------------------------------------------ 群额度
    async def _check_group_quota(self, event: AstrMessageEvent):
        """
        群聊每日 token 额度。

        额度是按群独立的：某个群刷爆了只停那个群，其它群不受影响。
        用量取自 AstrBot 的真实统计（provider_stats 表），不是估算。
        """
        limit = int(self.config.get("group_daily_token_limit", 0) or 0)
        if limit <= 0:
            return

        umo = _group_umo(event)
        if not umo:
            return  # 私聊不计入群额度

        # 记账：无论后面走真实查询还是兜底估算，都按字符累积一份。
        # 这样一旦真实查询失效，兜底数据是现成的，不用等到那时才开始统计。
        with contextlib.suppress(Exception):
            self._note_estimate(umo, len(event.message_str or ""))

        used = await self._group_used_tokens(umo)
        if used < limit:
            return

        # 额度用尽：拦下这条消息
        event.stop_event()
        event.should_call_llm(True)

        # 每个群只提示一次，避免刷屏（用内存集合，重启后最多再提示一次）
        if umo not in _QUOTA_NOTIFIED:
            _QUOTA_NOTIFIED.add(umo)
            note = str(self.config.get("group_limit_notify", "") or "").strip()
            if note:
                await event.send(event.plain_result(
                    note.replace("{limit}", str(limit))
                ))
        logger.info(
            f"[balance_guard] 群额度用尽 {umo}："
            f"{used}/{limit} tokens，已停止该群服务"
        )

    async def _group_used_tokens(self, umo: str) -> int:
        """
        查询某个群在本计费周期内已用 token 数（带短缓存）。

        两条数据来源：
          ① AstrBot 的 provider_stats 表 —— 真实用量，首选
          ② 本地按字符估算 —— 兜底

        为什么需要兜底：直接读 AstrBot 内部表是**没有公开契约**的做法，
        表结构或访问方式一变就全断。而群额度是花钱的保护措施，
        静默失效比不准更危险 —— 估得糙也比完全不限额好。
        """
        now = time.time()
        cached = _USAGE_CACHE.get(umo)
        if cached and (now - cached[0]) < _USAGE_CACHE_TTL:
            return cached[1]

        used = await self._query_provider_stats(umo)
        if used is None:
            # 表查不到：退化为按字符估算
            est = self._estimate_used_tokens(umo)
            if est > 0:
                if not _ESTIMATE_WARNED:
                    _ESTIMATE_WARNED = True
                    logger.warning(
                        "[balance_guard] 无法读取真实用量，已退化为按字符估算。"
                        "额度仍然生效，但数值不精确 —— 请检查上面的查询报错。"
                    )
                _USAGE_CACHE[umo] = (now, est)
                return est
            # 估算也没有：沿用上次的值，避免因为一次查询失败把群误封
            return cached[1] if cached else 0

        _USAGE_CACHE[umo] = (now, used)
        return used

    def _note_estimate(self, umo: str, chars: int):
        """
        记账：按字符累积估算用量。

        中文大致 1 token ≈ 1.5 字符，英文约 4 字符，这里取 1.7 折中。
        只统计用户消息 + 提示词是**估不准的**（真实请求还包含人设、历史、
        系统提示），所以这个数字系统性偏低 —— 它的作用是在读不到真实数据时
        仍然给出一个可用的上限，而不是精确计费。
        """
        if not umo or chars <= 0:
            return
        cur = _ESTIMATE.get(umo)
        today = datetime.now().strftime("%Y-%m-%d")
        if cur is None or cur[0] != today:
            _ESTIMATE[umo] = (today, int(chars / 1.7))
        else:
            _ESTIMATE[umo] = (today, cur[1] + int(chars / 1.7))

    def _estimate_used_tokens(self, umo: str) -> int:
        cur = _ESTIMATE.get(umo)
        if cur is None:
            return 0
        today = datetime.now().strftime("%Y-%m-%d")
        if cur[0] != today:
            return 0  # 跨天清零
        checkpoint = _ESTIMATE_CHECKPOINT.get(umo, 0)
        return max(0, cur[1] - checkpoint)

    def _window_start(self) -> datetime:
        """当前计费周期的起点（本地时区的某个整点）。"""
        hour = max(0, min(23, int(self.config.get("group_limit_reset_hour", 0) or 0)))
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        now_local = datetime.now(local_tz)
        start_local = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now_local < start_local:
            start_local -= timedelta(days=1)
        return start_local.astimezone(timezone.utc)

    async def _query_provider_stats(self, umo: str) -> int | None:
        """
        从 AstrBot 的 provider_stats 表汇总真实 token 用量。

        用的是公开的 SQLModel 表定义（astrbot/core/db/po.py::ProviderStat）。
        任何一步失败都返回 None —— 调用方会沿用旧值而不是把群封掉。
        """
        try:
            from sqlmodel import func, select

            from astrbot.core.db.po import ProviderStat
        except Exception as e:
            logger.warning(f"[balance_guard] 无法导入用量数据模型：{e}")
            return None

        try:
            db = self.context.get_db()
            if db is None:
                return None
        except Exception as e:
            logger.warning(f"[balance_guard] 取数据库失败：{e}")
            return None

        since = self._window_start()
        total = (
            func.coalesce(func.sum(ProviderStat.token_input_cached), 0)
            + func.coalesce(func.sum(ProviderStat.token_input_other), 0)
            + func.coalesce(func.sum(ProviderStat.token_output), 0)
        )
        stmt = select(total).where(
            ProviderStat.umo == umo,
            ProviderStat.created_at >= since,
        )
        checkpoint = await self._load_checkpoint(umo)
        try:
            # 注意：是 get_db()，不是 get_session()。
            # 它在 BaseDatabase 上是个 @asynccontextmanager，产出 AsyncSession：
            #     async with db.get_db() as session:
            #         ...
            async with db.get_db() as session:
                result = await session.execute(stmt)
                value = result.scalar()
        except Exception as e:
            logger.warning(f"[balance_guard] 用量查询失败：{e}")
            return None

        used = int(value or 0) - checkpoint
        return max(0, used)

    # ------------------------------------------------------------------ 额度重置
    async def _load_checkpoint(self, umo: str) -> int:
        """管理员手动重置额度时记下的扣减基线（绝对值）。"""
        data = await self._load_checkpoints()
        return int(data.get(umo, 0) or 0)

    async def _load_checkpoints(self) -> dict:
        try:
            data = await self.get_kv_data("quota_checkpoints", {})
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def _reset_group_usage(self, group_id: str) -> bool:
        """
        把某群的已用额度归零。

        做法是记下**当前窗口的累计总量**作为扣减基线（绝对值，不是增量 ——
        否则连续重置会重复累加，第二次就失效）。provider_stats 是框架的统计表，
        插件不该去改它，所以用基线扣减的方式实现"清零"。

        注意基线只对当前计费窗口有效：窗口滚过之后累计值重新变小，
        max(0, ...) 会把结果压回 0，不会出现负额度。
        """
        target = ""
        for key in list(_USAGE_CACHE.keys()):
            if key.endswith(f":{group_id}"):
                target = key
                break
        if not target:
            # 缓存里没有就用当前适配器的标准格式
            with contextlib.suppress(Exception):
                target = f"{self._platform_name()}:GroupMessage:{group_id}"
        if not target:
            return False

        used = await self._query_provider_stats(target)
        if used is None:
            # 真实查询不可用时，退化为重置估算基线 —— 否则估算模式下
            # 管理员的重置指令会完全无效
            cur = _ESTIMATE.get(target)
            if cur is None:
                return False
            _ESTIMATE_CHECKPOINT[target] = cur[1]
            _USAGE_CACHE.pop(target, None)
            _QUOTA_NOTIFIED.discard(target)
            logger.info(
                f"[balance_guard] 已重置群额度（估算模式）：{target}"
            )
            return True

        checkpoints = await self._load_checkpoints()
        checkpoints[target] = await self._raw_window_total(target)
        with contextlib.suppress(Exception):
            await self.put_kv_data("quota_checkpoints", checkpoints)

        _USAGE_CACHE.pop(target, None)
        _QUOTA_NOTIFIED.discard(target)
        logger.info(f"[balance_guard] 已按管理员指令重置群额度：{target}")
        return True

    async def _raw_window_total(self, umo: str) -> int:
        """当前窗口的累计总量（不加基线扣减），用于设置检查点。"""
        try:
            from sqlmodel import func, select

            from astrbot.core.db.po import ProviderStat
        except Exception:
            return 0
        try:
            db = self.context.get_db()
            if db is None:
                return 0
            total = (
                func.coalesce(func.sum(ProviderStat.token_input_cached), 0)
                + func.coalesce(func.sum(ProviderStat.token_input_other), 0)
                + func.coalesce(func.sum(ProviderStat.token_output), 0)
            )
            stmt = select(total).where(
                ProviderStat.umo == umo,
                ProviderStat.created_at >= self._window_start(),
            )
            async with db.get_db() as session:
                result = await session.execute(stmt)
                return int(result.scalar() or 0)
        except Exception as e:
            logger.warning(f"[balance_guard] 读取窗口累计失败：{e}")
            return 0

    def _platform_name(self) -> str:
        try:
            return str(self.context.get_platform() or "aiocqhttp")
        except Exception:
            return "aiocqhttp"

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """
        是否为管理员。

        判定顺序：配置里的 admin_qq > AstrBot 自己的权限判定。
        注意 event.is_admin() 依赖 AstrBot 侧的管理员设置，在私聊里行为可能
        与预期不同，所以配置项优先。
        """
        configured = str(self.config.get("admin_qq", "") or "").strip()
        if configured:
            try:
                return str(event.get_sender_id()) == configured
            except Exception:
                return False
        try:
            return bool(event.is_admin())
        except Exception:
            return False
