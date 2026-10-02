"""agent.core.obs —— 可观测性原语：请求标识贯穿 + 结构化日志 + 进程计数器。

为什么需要这一层（路线图 #5「结构化日志 + request id 贯穿」）：本项目原本
**「能审计、不能运维」**——核实过的三处缺口：

1. 审计脊柱 ``agent_events`` 只有 ``event/payload/at`` ⇒ **一次请求的痕迹无法串联**；
2. 全仓有 10+ 处 ``logging.getLogger(...)`` 却**没有任何 handler/basicConfig**
   ⇒ INFO 级静默丢弃、WARNING 走 Python 兜底 handler 输出裸文本、**无请求标识**；
3. uvicorn ``log_level="warning"`` ⇒ 访问日志近乎关闭。

本模块只提供**纯机制**（零业务词）三件事：

1. **请求标识贯穿**：``bind_request_id`` / ``ensure_request_id`` / ``current_request_id``
   （ContextVar，按 asyncio task 隔离 ⇒ 一个 HTTP 请求 = 一个 id）。手法与
   ``core/session.py`` 的会话键一致，故"同一次请求"在**日志与账本**两处口径相同。
2. **结构化日志**：``configure_logging`` 装 handler，并注入 ``LogRecordFactory`` 把
   ``rid/session/run`` **自动挂到每条记录**上 ⇒ 现有全部 ``logger.xxx()`` 调用点
   **零改动**即可带上请求标识（不指望任何人改写日志语句）。
   ``json`` 格式输出单行 JSON（可被采集侧直接解析），``text`` 给人看。
3. **进程计数器**：``inc_counter`` / ``counters_snapshot``。只做**无标签**计数
   ——刻意不做 per-session label（基数爆炸，见 ``/metrics`` 里 PRISM_METRICS_SESSION_LABELS
   的既有说明）；需要按请求排查时用 ``rid`` 过滤账本。
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from typing import Any

from .session import current_run_id, current_session_key

# 请求标识：按 asyncio task 隔离。默认空串（未绑定）⇒ 日志里显示 rid="" 而不是编造。
_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "prism_request_id", default="")

_TEXT_FORMAT = ("%(asctime)s %(levelname)-5s %(name)s "
                "rid=%(rid)s session=%(session)s run=%(run)s %(message)s")

_LOGGER_MARK = "_prism_obs"          # 在 handler 上打标记 ⇒ configure 可幂等重复调用
_COUNTERS: dict[str, float] = {}


# ---------- 请求标识 ----------

def new_request_id() -> str:
    """生成请求标识：``HHMMSS-<6 位随机>``（时间前缀便于人眼按序扫日志）。"""
    return f"{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}"


def bind_request_id(request_id: str | None = None) -> str:
    """把请求标识绑到**当前 task**（适配层中间件在每个 HTTP 入口调用）。

    语义分明：``None`` = **新生成**；``""`` = **显式解除绑定**（收尾与测试隔离用，
    不可与"生成"混为一谈——否则无法复位，绑定会顺着 ContextVar 泄漏到后续任务）。
    """
    rid = new_request_id() if request_id is None else str(request_id)
    _REQUEST_ID.set(rid)
    return rid


def ensure_request_id() -> str:
    """已有则原样返回，没有则生成并绑定（供无 HTTP 入口的路径用，如 ``--once``）。"""
    cur = _REQUEST_ID.get()
    return cur if cur else bind_request_id()


def current_request_id() -> str:
    """当前请求标识（未绑定 = 空串；**不编造**）。"""
    return _REQUEST_ID.get()


def trace_fields(ctx: Any = None) -> dict[str, str]:
    """当前请求的关联标识三元组（日志与账本**共用同一口径**）。

    ``ctx`` 只在 ContextVar 未绑定时用于回退读 ``ctx.state``；正常请求内两者都有值，
    以 ContextVar 为准（并发下不会串）。
    """
    return {
        "rid": _REQUEST_ID.get(),
        "session": current_session_key(ctx),
        "run": current_run_id(ctx),
    }


# ---------- 结构化日志 ----------

def _level(level: Any) -> int:
    if isinstance(level, int):
        return level
    return getattr(logging, str(level or "INFO").upper(), logging.INFO)


def _install_record_factory() -> None:
    """给每条记录补 ``rid/session/run``（已有 factory 上包一层，不重复安装）。

    为什么用 factory 而不是要求调用点拼参数：调用点有 10+ 处、遍布 core/sys/plugins/run，
    逐个改写等于把"可观测"变成一次大改造；而 factory 是**一处生效、零调用点改动**。
    """
    if getattr(logging.getLogRecordFactory(), "_prism_obs", False):
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if not getattr(record, "_prism_obs", False):
            record._prism_obs = True
            record.rid = _REQUEST_ID.get()
            record.session = current_session_key(None)
            record.run = current_run_id(None)
        return record

    logging.setLogRecordFactory(factory)


class JsonFormatter(logging.Formatter):
    """单行 JSON（采集侧可直接 ``json.loads``；异常带 ``exc`` 字段）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "rid": getattr(record, "rid", ""),
            "session": getattr(record, "session", ""),
            "run": getattr(record, "run", ""),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: Any = "INFO", fmt: str = "text",
                      stream: Any = None) -> logging.Logger:
    """装/更新 root 日志 handler（**幂等**：重复调用只改级别/格式/输出流，不叠加 handler）。

    装在 root 而非 ``agent.*`` logger 上：现存 logger 名为 ``agent.*`` 与 ``plugins.*``
    两族（``logging.getLogger("plugins.memory")`` 等），只配 ``agent`` 会漏掉后一族。
    显式传入 ``stream`` 时会**改写输出流**（否则重复调用无法把日志重定向到别处——
    幂等不等于"忽略参数"）。
    """
    root = logging.getLogger()
    lvl = _level(level)
    root.setLevel(lvl)
    handler = next((h for h in root.handlers if getattr(h, _LOGGER_MARK, False)), None)
    if handler is None:
        handler = logging.StreamHandler(stream or sys.stderr)
        setattr(handler, _LOGGER_MARK, True)
        root.addHandler(handler)
        _install_record_factory()
    elif stream is not None and getattr(handler, "stream", None) is not stream:
        handler.setStream(stream)
    handler.setFormatter(JsonFormatter() if str(fmt).lower() == "json"
                         else logging.Formatter(_TEXT_FORMAT))
    if lvl > logging.DEBUG:
        # 第三方库 INFO 是噪声（httpx 每次 LLM 调用打一行 URL）⇒ 常规级别下压制。
        # 刻意在 DEBUG 时**不**压制：显式要 DEBUG 的用户多半正在查网络层。
        for noisy in ("httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    return root


# ---------- 进程计数器（/metrics 用；无标签） ----------

def inc_counter(name: str, value: float = 1.0) -> None:
    """递增一个进程级计数（单事件循环内无需锁：dict 读写在 GIL 下原子）。"""
    _COUNTERS[name] = _COUNTERS.get(name, 0.0) + float(value)


def counters_snapshot() -> dict[str, float]:
    """当前所有计数的快照（``/metrics`` 渲染用；返回副本防外部改写）。"""
    return dict(_COUNTERS)


__all__ = ["new_request_id", "bind_request_id", "ensure_request_id",
           "current_request_id", "trace_fields", "configure_logging",
           "JsonFormatter", "inc_counter", "counters_snapshot"]
