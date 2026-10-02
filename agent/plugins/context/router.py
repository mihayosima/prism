"""agent.plugins.context.router —— 会话感知的治理器门面（G2 会话级）。

为什么是「门面」而不是「每会话一个容器」：见《上下文与预算会话级化-实施方案》§4 路线 B。
要点重述（代码即依据）：

- ``ContextGovernor`` 是**纯参数、零跨轮状态**对象（``from_config`` + 派生属性），
  ⇒ 会话级只需**按会话配置现造一个**，成本 ≈ 一次 dataclass 构造；
- 全部消费点都是 ``ctx.get(Services.CONTEXT)`` **现取**（loop / sys-chat / tasks / run-chat），
  ⇒ 只要这个服务的**值随会话变化**，消费方**零改动**；
- **无会话覆盖时门面恒返回容器实例** ⇒ 行为与改造前**逐值等价**（这是低风险的根本）。

当前会话键来源：``current_session_key()`` —— R1（2026-10-02）起是 **ContextVar 请求作用域
通道**（按 asyncio task 隔离 ⇒ 每请求独立；未绑定才回退 ``ctx.state``）。此前只读容器级
``StateKeys.SESSION_KEY``，并发请求交错时会被后写覆盖。原拟解法「请求级 child」经核实
不可行（读方是装配期捕获的容器 ctx + 事件总线纯度契约禁传 ctx + ``State`` 无父链），
详见《配置作用域治理/R1方案选型分析-2026-10-02.md》。

零业务语义纪律（继承本插件）：门面**不理解**任何治理语义，只做「选实例 + 转发」。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from agent.api import SESSION_CFG_VIEW, current_session_key

_DEFAULT_CACHE_LIMIT = 64


class GovernorRouter:
    """按会话键返回对应 ``ContextGovernor`` 的门面（duck-typed ``GovernorLike``）。

    - ``current()``：当前会话应使用的实例（容器实例或会话实例）；
    - 其余属性/方法经 ``__getattr__`` **转发**到 ``current()`` ⇒
      治理器新增方法时门面自动跟随，**协议漂移风险天然为零**；
    - 实例按「配置指纹」缓存（配置变了自动重建），缓存 LRU 上限 64。
    """

    def __init__(self, ctx, factory: Callable[[dict], Any], container_cfg: dict,
                 section: str = "context", cache_limit: int = _DEFAULT_CACHE_LIMIT) -> None:
        self._ctx = ctx
        self._factory = factory
        self._section = section
        self._limit = max(1, int(cache_limit or _DEFAULT_CACHE_LIMIT))
        self._cache: dict[str, Any] = {}
        # 容器实例：无会话覆盖时的唯一返回对象 ⇒ 与改造前同一个构造结果
        self._base_cfg = dict(container_cfg or {})
        self._base = factory(self._base_cfg)

    # ---- 会话键与配置 ----

    def _session_key(self) -> str:
        """当前会话键（R1：ContextVar 优先；未绑定回退容器 state；空串 = 容器级）。"""
        return current_session_key(self._ctx)

    def _cfg(self) -> dict:
        """当前会话的生效配置；无视图/无会话/取用失败 ⇒ 回落容器配置（**不劣化**）。"""
        sk = self._session_key()
        if not sk:
            return self._base_cfg
        try:
            view = self._ctx.get(SESSION_CFG_VIEW)
        except Exception:                    # noqa: BLE001 —— 接缝缺失即容器级
            return self._base_cfg
        if view is None:
            return self._base_cfg
        try:
            cfg = view(self._section, sk)
        except Exception:                    # noqa: BLE001 —— 视图异常不拖垮主链路
            return self._base_cfg
        return cfg or self._base_cfg

    # ---- 实例选择 ----

    def current(self):
        """当前会话的治理器实例（无覆盖时恒为容器实例）。"""
        cfg = self._cfg()
        if cfg is self._base_cfg:
            return self._base
        try:
            fp = json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:                    # noqa: BLE001 —— 不可序列化即每次现造
            return self._factory(cfg)
        hit = self._cache.get(fp)
        if hit is None:
            hit = self._factory(cfg)
            self._cache[fp] = hit
            if len(self._cache) > self._limit:
                # dict 保序：弹出最旧的（配置指纹维度，与会话数无关）
                self._cache.pop(next(iter(self._cache)), None)
        return hit

    # ---- 转发 ----

    def __getattr__(self, name: str):
        # 只在常规查找失败时触发 ⇒ self._base / _cache 等私有属性不受影响；
        # 未初始化完毕（__init__ 之前被取属性）时避免递归。
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.current(), name)


__all__ = ["GovernorRouter"]
