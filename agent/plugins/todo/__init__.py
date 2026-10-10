"""插件包：todo —— 结构化待办/规划（P2-1，TodoWrite 式）。

把 P0-2 的「纯文本规划纪律」升级为真实机制：模型经工具 ``todo.write`` / ``todo.list``
维护结构化待办，``TodoStore`` 持有状态，mutation 后 emit ``Events.TODO_PROGRESS``
供前端进度条消费。

设计立场：``loop.py`` 零改动；工具经 ``ctx.tasks.register`` 暴露（与 approval/subagent
同构）；进度帧经 ``sys/progress`` 帧桥流入 SSE。``risk="read"``：本插件只改 agent 自身的
内存计划态、无外部副作用，且必须在 plan 权限模式下可见（``loop.py:245`` 的 plan 过滤只
放行 ``risk=="read"`` —— 反向印证 plan 软可见性过滤过粗：隐藏 write 工具对规划无安全价值）。
"""

from __future__ import annotations

from agent.api import (
    Events,
    Services,
    current_session_key,
    plugin,
)

from .store import TodoStore


@plugin
class TodoWrite:
    """ctx.todo —— 结构化待办/规划：列计划、跟踪完成度、暴露前端进度条。"""

    name = "plugins.todo"
    version = "0.1.0"
    layer = "business"
    provides = [Services.TODO]
    # tasks：注册 todo.write / todo.list；events：emit TODO_PROGRESS。
    inject = [Services.TASKS, Services.EVENTS]
    tables = ()                     # 会话内存态，不落库 ⇒ 卸载零残留

    async def apply(self, ctx) -> None:
        self._ctx = ctx
        self._store = TodoStore()
        ctx.effect(self._dispose)
        ctx.tasks.register(
            ctx, "todo.write",
            schema={
                "todos": {
                    "type": "array", "required": True,
                    "description": (
                        "完整待办列表（**整表替换**：每次调用整体替换旧状态，"
                        "天然支持重排序与删除）。每项字段："
                        "content(祈使句,必填) / status("
                        "pending|in_progress|completed|blocked) / "
                        "activeForm(进行时短语,进度条展示) / id(可选,留空自动生成)。")}},
            handler=self._write, owner="plugins.todo",
            description=(
                "维护结构化待办清单（整体替换）。非平凡多步任务（≥3 步/跨文件/多调用点/"
                "多子问题）开始前先调用列计划；每完成一步把对应 status 置 completed、"
                "把下一项置 in_progress；无关项直接移除；卡住时保持 in_progress 并新增"
                "一条描述卡点的任务，绝不草率标 completed。用户端可见实时进度条。"
                "plan 模式下亦可调用。"),
            risk="read")
        ctx.tasks.register(
            ctx, "todo.list",
            schema={},
            handler=self._list, owner="plugins.todo",
            description="回读当前待办清单与完成度摘要", risk="read")
        ctx.provide(Services.TODO, self)

    # ---------- 工具处理 ----------

    async def _write(self, todos: list | None = None, **_) -> dict:
        sk = current_session_key(self._ctx) or "default"
        summary = self._store.write(sk, todos or [])
        try:
            await self._ctx.events.emit(Events.TODO_PROGRESS, summary)   # live 遥测，不落库
        except Exception:                                                # 遥测失败不阻断主链路
            pass
        return {"ok": True, **summary}

    async def _list(self, **_) -> dict:
        sk = current_session_key(self._ctx) or "default"
        return {"todos": self._store.list(sk), **self._store.summary(sk)}

    # ---------- 查询接缝（供 react / 测试读取） ----------

    async def list_tasks(self, ctx) -> dict:
        """按当前会话读待办 + 完成度（react 与测试共用）。"""
        sk = current_session_key(ctx) or "default"
        return {"todos": self._store.list(sk), **self._store.summary(sk)}

    async def status(self, ctx) -> dict:
        """插件页可观测（仿 approval.status）。"""
        sk = current_session_key(ctx) or "default"
        return {"session": sk, **self._store.summary(sk)}

    async def _dispose(self) -> bool:
        self._store = None
        return True
