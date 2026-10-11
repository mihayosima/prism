"""agent.core.permissions —— 权限档位（PERMISSION_MODE）的读写助手（内核层）。

为什么收在内核：该键有**两个互不相识的消费方**——approval（判定闸门）与 react
（工具可见性裁剪 + 状态上报）。若各自判断值形态，就会出现第二处实现（形状 SSOT 破裂）。

R1 补（2026-10-02）：值由**单值 str** 改为 ``dict[会话键, mode]``（与 BUDGET_SPENT 同款
分桶）——此前是「会话级语义 + 容器级存储」⇒ A 会话切 bypass 会放行 B 会话在跑 run 的
审批闸。读写两侧经本处归一，**已存在的 str 旧值仍被识别**（视作容器级默认）以兼容直接
写 state 的既有调用点与测试。

2026-10-11 下沉：由 ``agent/api.py`` 移入内核，使 ``core/skills_catalog.py`` 也能经内核
取用（api 不得反向依赖 sys，而插件/传输层又只能经 api 取用 ⇒ 共用原语必须落在 core）。
``agent.api`` 原样再导出，所有既有调用点不受影响。
"""

from __future__ import annotations

from agent.core.session import current_session_key
from agent.core.state_keys import StateKeys

_PERMISSION_BUCKET_LIMIT = 512          # 只防长期运行无限增长（同 budget 的桶上限口径）


def current_permission_mode(ctx, default: str = "default") -> str:
    """当前**会话**的权限档位（跨插件共享键的形状容错读取，SSOT）。

    解析顺序：``dict`` 桶中本会话的值 → 整值为 ``str`` 的旧形状（视作容器级默认）
    → ``default``。读取异常一律退 ``default``（绝不阻断主链路）。
    """
    try:
        raw = (getattr(ctx, "state", None) or {}).get(StateKeys.PERMISSION_MODE)
    except Exception:                   # noqa: BLE001
        return str(default)
    if isinstance(raw, dict):
        return str(raw.get(current_session_key(ctx)) or default)
    if isinstance(raw, str) and raw:
        return raw
    return str(default)


def set_permission_mode(ctx, mode: str) -> None:
    """把**当前会话**的档位写入分桶（整值替换，不做原地改 ⇒ 并发下无半更新态）。

    非 ``dict`` 的旧值按容器级默认丢弃（该旧值只可能是单值形态，无法归属到会话）。
    """
    sk = current_session_key(ctx)
    try:
        raw = (getattr(ctx, "state", None) or {}).get(StateKeys.PERMISSION_MODE)
    except Exception:                   # noqa: BLE001
        raw = None
    bucket = dict(raw) if isinstance(raw, dict) else {}
    bucket.pop(sk, None)                          # 重插以把本会话推到"最新"侧
    bucket[sk] = str(mode)
    while len(bucket) > _PERMISSION_BUCKET_LIMIT:
        bucket.pop(next(iter(bucket)), None)      # dict 保序：最早插入者先淘汰
    ctx.state[StateKeys.PERMISSION_MODE] = bucket


__all__ = ["current_permission_mode", "set_permission_mode"]
