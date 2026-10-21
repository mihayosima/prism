"""agent.core.permissions —— 权限档位的读写助手。

为什么收在内核：该键有两个互不相识的消费方——approval 与 react
。若各自判断值形态，就会出现第二处实现。

R1 补：值由单值 str 改为 ``dict[会话键, mode]``——此前是「会话级语义 + 容器级存储」⇒ A 会话切 bypass 会放行 B 会话在跑 run 的
审批闸。读写两侧经本处归一，已存在的 str 旧值仍被识别以兼容直接
写 state 的既有调用点与测试。
"""

from __future__ import annotations

from agent.core.session import current_session_key
from agent.core.state_keys import StateKeys

_PERMISSION_BUCKET_LIMIT = 512          # 只防长期运行无限增长


def current_permission_mode(ctx, default: str = "default") -> str:
    """当前会话的权限档位。

 解析顺序：``dict`` 桶中本会话的值 → 整值为 ``str`` 的旧形状
 → ``default``。读取异常一律退 ``default``。
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
    """把当前会话的档位写入分桶。

 非 ``dict`` 的旧值按容器级默认丢弃。
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
