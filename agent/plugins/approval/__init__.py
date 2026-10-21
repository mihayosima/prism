"""插件包：approval —— 人在环中与权限模式。

职责三分：
- mode：本插件持有，写
 ``ctx.state["permission_mode"]`` —— R1 补起值是 ``dict[会话键, mode]``。读写一律经内核助手 ``current_permission_mode()`` / ``set_permission_mode()``。
- policy：``policy.py`` 纯函数；
- provider：``providers.py`` 可换接缝。

挂点与优先级：
 默认 append 挂 ``agent/tasks/pre-execute`` ⇒ 排 guard 之后。
 guard 硬否决短路 ⇒ approval 不被调用；
 guard 放行 ⇒ 调 ``next()`` ⇒ approval 按自身名单 + 模式判定。
 ask 的来源是本插件名单与模式；guard 的 ``permissions.risk.*="ask"``
 是无本插件时的 fail-safe 兜底配置，两者不应同时使用。

fail-safe 铁律：ask 无人应答 / 超时 / provider 异常 / 插件停用 ⇒ 一律默认拒绝。

默认配置下行为与无本插件时完全一致，便于灰度。
"""

from __future__ import annotations

import time
import uuid

from agent.api import (AgentError, Events, Services, StateKeys, current_session_key,
                       durable, now_iso, current_permission_mode, plugin,
                       set_permission_mode)

from .options import normalize_options
from .pending import PendingTable
from .policy import MODES, decide
from .providers import build_provider

# 插件自管默认：
# allow=["*"] ⇒ 默认全放行；default="deny" ⇒ 收窄后未命中即拒。
DEFAULT_CONFIG: dict = {
    "provider": "auto-deny",
    "allow": ["*"],
    "deny": [],
    "ask": [],
    "timeout_s": 30,
    "default": "deny",
}

# : 会话级名单的会话数上限。
# : 只防长期运行下「会话数 × 名单」无限增长；淘汰最久未用者。
_SESSION_BUCKET_LIMIT = 512


def _remember(bucket: dict, session: str, task: str) -> None:
    """把 ``task`` 记入 ``session`` 的名单桶，并限总量。"""
    bucket.setdefault(session, set()).add(task)
    while len(bucket) > _SESSION_BUCKET_LIMIT:
        bucket.pop(next(iter(bucket)), None)      # dict 保序：最早插入的会话先淘汰


def _picked(ans: dict) -> list[str]:
    """把应答里的单选 ``choice`` / 多选 ``choices`` 统一成取值列表。"""
    raw = ans.get("choices")
    if raw is None:
        raw = ans.get("choice")
    items = list(raw) if isinstance(raw, (list, tuple)) else ([raw] if raw is not None else [])
    out: list[str] = []
    for it in items:
        s = str(it).strip() if it is not None else ""
        if s and s not in out:
            out.append(s[:200])
    return out


@plugin
class Approval:
    """ctx.approval —— 人在环中审批 + 权限模式。"""

    name = "plugins.approval"
    version = "0.4.0"
    layer = "system"
    provides = [Services.APPROVAL]
    # 补 ``Services.TASKS``：``apply``/``_sync_submit_plan`` 里注册钩子用
    # ``ctx.tasks.register(...)``，此前读了却没声明 ⇒ 装配顺序只是"碰巧对"
    # 不是契约保证。``tasks`` 由 ``SYS_PLUGINS`` 常驻提供，
    # 声明它不会破坏 opt-in。
    inject = [Services.EVENTS, Services.DB, Services.TASKS]
    tables = ()                     # 审计走现成 agent_events，不自建表 ⇒ 卸载零残留

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("approval") or {}
        self._cfg: dict = {**DEFAULT_CONFIG, **cfg}
        self._ctx = ctx
        self._pending = PendingTable(self._cfg.get("timeout_s", DEFAULT_CONFIG["timeout_s"]))
        self._provider = build_provider(self._cfg.get("provider"), table=self._pending)
        # 第 3 期：会话级批量确认
        # 2.2：按会话分桶 —— 此前是全局单 set，A 的「始终允许」会泄漏给 B。
        self._session_allow: dict[str, set[str]] = {}
        self._session_deny: dict[str, set[str]] = {}
        self._plan_dispose = None          # submit_plan 动态注册句柄
        # R1 补：档位已按会话分桶 ⇒ 工具注册状态也必须按会话计数，否则 A 离开 plan
        # 会把 B 的终态工具注销。真正的越权防线是 _submit_plan 的硬兜底自拒。
        self._plan_sessions: set[str] = set()
        # 默认 append：排 guard 之后——硬规则先裁、问人后行
        ctx.on(Events.TASKS_PRE_EXECUTE, self._pre_task)
        ctx.effect(self._pending.aclose)
        ctx.provide(Services.APPROVAL, self)
        # 容器级默认桶；旧单值形态仍被 current_permission_mode() 认作默认
        ctx.state.setdefault(StateKeys.PERMISSION_MODE, {})
        # 澄清问询工具：模型不确定用户意图时
        # 主动发卡片等人指示——人机交互不止于「批准/拒绝」二元。
        self._ask_dispose = ctx.tasks.register(
            ctx, "approval.ask_user",
            schema={
                "question": {"type": "str", "required": True,
                             "description": "要问用户的问题（具体、可决策）"},
                "options": {"type": "str", "required": False, "default": "",
                            "description": (
                                "候选方案。**推荐传 JSON 数组字符串**，元素可为纯文本或对象；"
                                "对象字段：label（选项名，必填）、desc（一句话说明为什么可行）、"
                                "value（回传值，缺省同 label）、group（分组名，用于按维度分区）。"
                                '例：\'[{"label":"方案A","desc":"改动最小","group":"技术"},'
                                '{"label":"方案B","desc":"能力最强","group":"产品"}]\'。'
                                "也兼容逗号/换行分隔的纯文本（如 方案A,方案B）")},
                "multi": {"type": "bool", "required": False, "default": False,
                          "description": ("是否允许多选（默认单选）。多选时用户勾选若干项后"
                                          "一次提交，你会收到 choices 列表")},
                "allow_free_text": {"type": "bool", "required": False, "default": True,
                                    "description": "是否允许用户自由输入文字补充指示"},
            },
            handler=self._ask_user, owner="plugins.approval",
            description=("当你不确定用户想要哪个方案/方向时，调用此工具向用户提问："
                         "会弹出交互卡片，用户可点选方案或输入文字指示，答复前执行暂停；"
                         "候选较多或需要说明差异时，用 options 的 JSON 对象数组给出"
                         "label + desc，需要用户同时定多个点时把 multi 设为 true；"
                         "超时未答将收到未应答通知，届时按最合理假设继续并说明假设"),
            risk="read")

    # ---------- 能力闸 ----------

    async def _pre_task(self, payload: dict, next) -> dict:
        ctx = self._ctx
        mode = self.mode()
        if mode == "bypass":                        # bypass：不打扰
            return await next()
        task = str(payload.get("task", ""))
        risk = str(payload.get("risk", ""))
        decision = decide(self._cfg, task, mode=mode, risk=risk)
        if decision == "allow":
            return await next()
        call_id = uuid.uuid4().hex
        started = time.monotonic()
        if decision == "deny":
            reason = (f"approval: 任务 {task!r} 为非只读，plan 模式下被拒"
                      if mode == "plan" else
                      f"approval: 任务 {task!r} 命中 deny 名单")
            await self._audit(Events.APPROVAL_ANSWERED, {
                "call_id": call_id, "tool": task, "approved": False,
                "by": "policy:deny", "waited_ms": 0})
            return self._denied(task, call_id, reason, denied_by="approval")
        # ---- ask：会话级批量确认→ 问人 ----
        # 2.2：名单按当前会话取桶
        session = self._skey()
        if task in self._session_deny.get(session, ()):
            await self._audit(Events.APPROVAL_ANSWERED, {
                "call_id": call_id, "tool": task, "approved": False,
                "by": "session-deny", "waited_ms": 0})
            return self._denied(task, call_id,
                                f"approval: 任务 {task!r} 被用户设为本会话拒绝",
                                denied_by="approval")
        if task in self._session_allow.get(session, ()):
            await self._audit(Events.APPROVAL_ANSWERED, {
                "call_id": call_id, "tool": task, "approved": True,
                "by": "session-allow", "waited_ms": 0})
            return await next()
        req = {"call_id": call_id, "tool": task, "risk": risk, "mode": mode,
               "session": session, "kwargs": payload.get("kwargs") or {}}
        await self._audit(Events.APPROVAL_ASKED,
                          {"call_id": call_id, "tool": task, "risk": risk, "mode": mode})
        # 仅交互式 provider 发弹窗信号
        interactive = bool(getattr(self._provider, "interactive", False))
        if interactive:
            try:
                await ctx.events.emit(Events.APPROVAL_PENDING, req)
            except Exception:                       # noqa: BLE001 —— 遥测失败不阻断审批
                pass
        try:
            ans = await self._provider.ask(ctx, req)
        except Exception:                           # noqa: BLE001 —— provider 崩了也必须拒
            ans = {"approved": False, "by": "provider-error"}
        waited_ms = int((time.monotonic() - started) * 1000)
        approved = bool(ans.get("approved"))
        by = str(ans.get("by", "unknown"))
        # 批量确认：应答带 always ⇒ 记入会话级名单
        # 2.2：写回本会话的桶
        if by == "user" and ans.get("always"):
            bucket = (self._session_allow if approved else self._session_deny)
            _remember(bucket, session, task)      # 记入本会话名单，含总量上限，防无界增长
        if by == "timeout":
            await self._audit(Events.APPROVAL_TIMEOUT,
                              {"call_id": call_id, "tool": task, "waited_ms": waited_ms})
        await self._audit(Events.APPROVAL_ANSWERED, {
            "call_id": call_id, "tool": task, "approved": approved,
            "by": by, "waited_ms": waited_ms})
        if interactive:                             # 收尾信号：前端弹窗据此消隐
            try:
                await ctx.events.emit(Events.APPROVAL_ANSWERED, {
                    "call_id": call_id, "tool": task, "approved": approved, "by": by})
            except Exception:                       # noqa: BLE001 —— 遥测失败不阻断主链路
                pass
        if not approved:
            return self._denied(task, call_id,
                                f"approval: 任务 {task!r} 未获批准（by={by}）",
                                denied_by="user" if by == "user" else "approval")
        # updated_input：人修订了参数 ⇒ 回读给 invoke
        rw = ans.get("kwargs")
        if isinstance(rw, dict) and rw != (payload.get("kwargs") or {}):
            return {"kwargs": rw, "denied": False,
                    "reason": "approval: 参数已经人修订", "denied_by": "approval"}
        return await next()

    # ---------- 会话归属 ----------

    def _skey(self) -> str:
        """当前会话键。

                 R1：读取通道是 ``current_session_key()`` —— ContextVar 优先，未绑定才回退容器级 ``ctx.state``；未解析到时退
                 ``"default"``。真正的会话隔离靠待答表与名单的分桶，此键回答「此刻是哪个会话」。

                 本读取点没有 ctx 入参⇒ 请求作用域通道是唯一
                 可达方案；此前读容器级 state 会被并发请求的后写覆盖。
                 """
        return current_session_key(self._ctx) or "default"

    # ---------- 会话模式 ----------

    def mode(self) -> str:
        """当前会话档位。

         R1 补：经内核助手读 ``dict[会话键, mode]`` 桶中本会话的值；
         此前读容器级单值 ⇒ 档位跨会话互相覆盖。
         """
        return current_permission_mode(self._ctx)

    async def set_mode(self, ctx, mode: str, *, by: str = "user") -> None:
        """切换当前会话档位并落审计。OPEN-5：切 bypass 的二次确认属第 2 期交互。

         会话归属来自请求作用域通道；未绑定
         时退容器键 ``"default"`` ⇒ 行为与单会话时期一致。
         """
        if mode not in MODES:
            raise AgentError(f"mode 须 ∈ {MODES}，得到 {mode!r}")
        old = self.mode()
        set_permission_mode(ctx, mode)
        self._sync_submit_plan(ctx, mode)
        await self._audit(Events.APPROVAL_MODE_CHANGED,
                          {"from": old, "to": mode, "by": by,
                           "session": self._skey()})

    # ---------- plan 终态工具 ----------

    def _sync_submit_plan(self, ctx, mode: str) -> None:
        """终态工具 ``submit_plan`` 的可达性：有会话在 plan 即注册，全都离开才注销。「仅 plan 可见」在容器级注册表里的最近似实现。
         R1 补：档位按会话分桶后，注册状态按会话计数——否则 A 切回 default
         会把正在 plan 的 B 的终态工具一并注销。
         残留：只要有会话在 plan，该工具对其它会话也可见。
         越权面已被 ``_submit_plan`` 的硬兜底关死，可见性只是
         多一个会自拒的只读工具；要彻底按会话隔离可见性，须给注册表加"按会话可见"维度，
         属新能力，不在本批。
         """
        sk = self._skey()
        if mode == "plan":
            self._plan_sessions.add(sk)
        else:
            self._plan_sessions.discard(sk)
        want = bool(self._plan_sessions)
        if want and self._plan_dispose is None:
            self._plan_dispose = ctx.tasks.register(
                ctx, "approval.submit_plan",
                schema={"plan": {"type": "string", "required": True,
                                 "description": "完整执行计划文本"}},
                handler=self._submit_plan, owner="plugins.approval",
                description="提交执行计划以获取用户批准（仅 plan 模式可见）",
                risk="read")
        elif not want and self._plan_dispose is not None:
            self._plan_dispose()
            self._plan_dispose = None

    async def _submit_plan(self, plan: str = "", **_) -> dict:
        """模型提交计划 → 问人批准：同意 ⇒ 切 auto；拒绝 ⇒ 留 plan 回灌意见。
         问人复用 provider/待答表通道，超时按 fail-safe 拒绝。"""
        ctx = self._ctx
        if self.mode() != "plan":
            return {"approved": False, "hint": "当前不在 plan 模式，无需提交计划"}
        call_id = uuid.uuid4().hex
        started = time.monotonic()
        await self._audit(Events.APPROVAL_ASKED, {
            "call_id": call_id, "tool": "approval.submit_plan",
            "risk": "read", "mode": "plan"})
        interactive = bool(getattr(self._provider, "interactive", False))
        req = {"call_id": call_id, "tool": "approval.submit_plan", "risk": "read",
               "mode": "plan", "kwargs": {"plan": plan}}
        if interactive:
            try:
                await ctx.events.emit(Events.APPROVAL_PENDING, req)
            except Exception:                       # noqa: BLE001 —— 遥测失败不阻断审批
                pass
        try:
            ans = await self._provider.ask(ctx, req)
        except Exception:                           # noqa: BLE001 —— provider 崩了也必须拒
            ans = {"approved": False, "by": "provider-error"}
        approved = bool(ans.get("approved"))
        by = str(ans.get("by", "unknown"))
        await self._audit(Events.APPROVAL_ANSWERED, {
            "call_id": call_id, "tool": "approval.submit_plan", "approved": approved,
            "by": by, "waited_ms": int((time.monotonic() - started) * 1000)})
        if interactive:
            try:
                await ctx.events.emit(Events.APPROVAL_ANSWERED, {
                    "call_id": call_id, "tool": "approval.submit_plan",
                    "approved": approved, "by": by})
            except Exception:                       # noqa: BLE001 —— 遥测失败不阻断主链路
                pass
        if not approved:
            note = str(ans.get("note") or "").strip()
            return {"approved": False,
                    "hint": "用户未批准该计划" + (f"，反馈：{note}" if note else "")
                            + "；请按反馈调整后重新调用 submit_plan"}
        await self.set_mode(ctx, "auto", by="plan-approval")
        return {"approved": True, "mode": "auto",
                "hint": "计划已批准，模式已切至 auto；请输出最终答复（执行将在后续请求中进行）"}

    # ---------- 澄清问询 ----------

    async def _ask_user(self, question: str = "", options: str = "",
                        multi: bool = False, allow_free_text: bool = True, **_) -> dict:
        """模型主动问用户：发交互卡片，等答复后回灌。

         ``options`` 经 ``normalize_options`` 归一为选择规格，
         卡片据此渲染形态匹配的选择栏，而非把所有候选
         压进同一个扁平选择栏；``multi=True`` 时用户可勾选多项，回灌 ``choices`` 列表。

         fail-safe：非交互通道⇒ 立即告知模型自行假设；
         超时/未应答 ⇒ 返回 ``answered=False`` 并附假设提示，模型可继续推进。
         """
        ctx = self._ctx
        interactive = bool(getattr(self._provider, "interactive", False))
        if not interactive:
            return {"answered": False,
                    "reason": (f"当前问人通道为非交互（provider={self._cfg.get('provider')}），"
                               "无法等待用户输入；请按最合理假设继续，并在答复中说明该假设")}
        call_id = uuid.uuid4().hex
        started = time.monotonic()
        opts = normalize_options(options)
        is_multi = bool(multi) and bool(opts)          # 无候选时多选无意义，退回单选语义
        req = {"call_id": call_id, "kind": "ask_user", "question": question,
               "options": opts, "multi": is_multi, "session": self._skey(),
               "allow_free_text": bool(allow_free_text)}
        await self._audit(Events.APPROVAL_ASKED,
                          {"call_id": call_id, "tool": "approval.ask_user",
                           "risk": "read", "mode": self.mode(), "question": question,
                           "multi": is_multi,
                           "options": [o["label"] for o in opts]})
        try:
            await ctx.events.emit(Events.ASK_USER_PENDING, req)
        except Exception:                           # noqa: BLE001 —— 遥测失败不阻断问询
            pass
        try:
            ans, _ = await self._pending.wait(f"ask_user:{call_id}", req,
                                              call_id=call_id, session=self._skey())
        except Exception:                           # noqa: BLE001 —— 通道崩了也不能挂死循环
            ans = {"by": "provider-error"}
        waited_ms = int((time.monotonic() - started) * 1000)
        by = str(ans.get("by", "unknown"))
        answered = bool(ans.get("answered"))
        picked = _picked(ans)                       # 单选 choice / 多选 choices 统一为列表
        choice = picked[0] if len(picked) == 1 else None
        text = str(ans.get("text") or "").strip()
        await self._audit(Events.APPROVAL_ANSWERED, {
            "call_id": call_id, "tool": "approval.ask_user", "approved": answered,
            "by": by, "waited_ms": waited_ms, "choice": choice,
            "choices": picked, "text": text})
        try:
            await ctx.events.emit(Events.ASK_USER_ANSWERED,
                                  {"call_id": call_id, "answered": answered, "by": by})
        except Exception:                           # noqa: BLE001 —— 遥测失败不阻断主链路
            pass
        if not answered and not text and not picked:
            reason = ("用户未应答（超时）" if by == "timeout"
                      else "用户未给出有效答复" if by == "user"
                      else f"问询通道异常（by={by}）")
            return {"answered": False,
                    "reason": f"{reason}；请按最合理假设继续，并在答复中明确说明该假设"}
        out: dict = {"answered": True, "by": by, "choices": picked, "text": text}
        if choice is not None:
            out["choice"] = choice                      # 单项答复：额外给单值，便于模型直取
        if is_multi:
            out["multi"] = True
        out["hint"] = ("已获得用户指示，请严格按其继续" if len(picked) <= 1
                       else "用户同时选了多项，请一并满足；相互冲突时以先列出者为主")
        return out

    # ---------- 待答表 ----------

    @property
    def pending(self) -> PendingTable:
        return self._pending

    # ---------- 插件页可观测 ----------

    async def status(self, ctx) -> dict:
        return {"mode": self.mode(), "provider": self._cfg.get("provider"),
                "ask_rules": len(self._cfg.get("ask") or []),
                "deny_rules": len(self._cfg.get("deny") or []),
                "pending": self._pending.size(),
                # 2.2：名单按会话分桶 ⇒ 展示当前会话的条目数
                "session_allow": len(self._session_allow.get(self._skey(), ())),
                "session_deny": len(self._session_deny.get(self._skey(), ())),
                "plan_tool": self._plan_dispose is not None}

    # ---------- 内部 ----------

    @staticmethod
    def _denied(task: str, call_id: str, reason: str, *, denied_by: str = "approval") -> dict:
        """拒绝决策。``denied_by``：``"user"``=用户本轮主动拒、
         ``"approval"``=策略拒绝。"""
        return {"denied": True, "reason": reason, "denied_by": denied_by,
                "approval": {"call_id": call_id, "tool": task}}

    async def _audit(self, event: str, payload: dict) -> None:
        try:
            await durable(self._ctx, event, {**payload, "at": now_iso()})
        except Exception:                           # noqa: BLE001 —— 审计失败不阻断主链路
            pass


__all__ = ["Approval", "DEFAULT_CONFIG"]
