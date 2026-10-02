"""#5 可观测性：请求标识贯穿 + 账本可串联 + 结构化日志（路线图 #5 验收）。

实施前的真实缺口（核实过，不是推测）：

1. ``agent_events`` 只有 ``event/payload/at`` ⇒ **一次请求的痕迹无法串联**；
2. 全仓 10+ 处 ``logging.getLogger(...)`` 却**无任何 handler/basicConfig**
   ⇒ INFO 静默丢弃、WARNING 走 Python 兜底 handler 输出裸文本、**无请求标识**；
3. uvicorn ``log_level="warning"`` 硬编码 ⇒ 访问日志近乎关闭。

**为什么本文件不发真实 HTTP 请求**：``_serve`` 返回时容器已 teardown（``ctx`` 关闭 ⇒
服务读取报「服务未提供」），进程内拿不到可用的 app——``test_run_http_resilience`` 之所以
能取到 app，是因为它只断言**注册项**（handler/路由），不发请求。
故本文件分两层：
- **进程内**：直接调用生产中间件 ``run/chat._obs_middleware``（不是复刻），
  账本/日志/计数器/存量库补列用真容器验证；
- **端到端**（真实服务 + 真实 HTTP 请求，含 ``X-Request-Id`` 响应头与
  ``/api/events?request_id=`` 过滤）：``.workbuddy/obs_live_check.py``。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sqlite3

import pytest

from agent.api import (StateKeys, bind_request_id, configure_logging,
                       counters_snapshot, current_request_id, durable,
                       ensure_request_id, inc_counter, new_request_id,
                       set_current_run_id, set_current_session_key)
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager
from agent.tables import MIGRATIONS

from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
PLUGIN_DIR = AGENT_DIR / "plugins"


def _run(coro):
    return asyncio.run(coro)


def _container(tmp_path, name="a.db"):
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR), state_file=str(tmp_path / "s.json"))
    return build_agent(config={"db_path": str(tmp_path / name),
                               "agent": {"llm": {"provider": "fixture"}}},
                       plugin_mgr=pm)


@pytest.fixture(autouse=True)
def _isolate_request_scope():
    """每个用例前后**解除**请求作用域的三项绑定（request id / session / run）。

    ContextVar 绑定在**当前线程**的上下文里 ⇒ 一个用例里绑定后，后续用例会继承
    （``asyncio.run`` 复制当前上下文），表现为「未绑定却读到上一个用例的值」这类
    **跨用例串扰**——本文件真踩过：日志用例绑了 ``session=u:log``，导致随后
    ``test_session_scope`` 的会话断言读到 ``u:log``（``assert - u:A / + u:log``）。
    故显式复位；``None`` = 解除绑定（回到"未绑定 ⇒ 回退 state"）。
    """
    bind_request_id("")
    set_current_session_key(None)
    set_current_run_id(None)
    yield
    bind_request_id("")
    set_current_session_key(None)
    set_current_run_id(None)


# ---------------------------------------------------------------------------
# ① 请求标识原语
# ---------------------------------------------------------------------------

def test_request_id_is_generated_and_task_scoped():
    """每个请求一个 id；同一 task 内读得到；两个 task 互不串。"""
    async def s():
        first = bind_request_id()
        assert first and first == current_request_id()
        assert ensure_request_id() == first, "已绑定时不得换 id"

        async def other():
            return bind_request_id()

        second = await asyncio.create_task(other())
        assert second != first, "派生 task 拿到了同一个 id ⇒ 并发会串链"
        assert current_request_id() == first, "子 task 的绑定泄漏回了父 task"
    _run(s())


def test_ensure_request_id_fills_unbound_context():
    """未绑定时生成一个（供 ``--once`` 等无 HTTP 入口的路径），不为空也不编造语义。"""
    rid = ensure_request_id()
    assert rid and rid == current_request_id()
    assert new_request_id() != new_request_id(), "id 必须逐次不同"


def test_new_request_id_shape_is_human_sortable():
    """``HHMMSS-<6 位>``：人眼按序扫日志，且长度可控（要塞进响应头与日志行）。"""
    rid = new_request_id()
    head, _, tail = rid.partition("-")
    assert len(head) == 6 and head.isdigit(), rid
    assert len(tail) == 6 and all(ch in "0123456789abcdef" for ch in tail), rid


# ---------------------------------------------------------------------------
# ② 中间件（直接调用生产实现，不复刻）
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.headers: dict = {}


class _Url:
    def __init__(self, path: str):
        self.path = path


class _Req:
    """最小请求替身：中间件只用到 method 与 url.path（访问日志需要）。"""

    def __init__(self, method: str = "GET", path: str = "/api/status"):
        self.method = method
        self.url = _Url(path)


def test_middleware_binds_id_before_calling_next_and_sets_header():
    """绑定必须发生在 ``call_next`` **之前**——否则下游（路由/其派生 task/异步监听器）
    读不到本请求的 id，账本就串不起来。"""
    from agent.run.chat import _obs_middleware

    seen: list[str] = []

    async def call_next(_request):
        seen.append(current_request_id())
        return _Resp(200)

    resp = _run(_obs_middleware(_Req(), call_next))
    assert seen and seen[0], "进入下游时 id 尚未绑定"
    assert resp.headers["X-Request-Id"] == seen[0]


def test_middleware_writes_one_access_log_line_with_rid(root_logger_sandbox):
    """每请求一行访问日志，且**行内含 rid** ⇒ 排查时先 grep rid 就能定位到那一轮。"""
    from agent.run.chat import _obs_middleware

    stream = io.StringIO()
    configure_logging(level="INFO", fmt="json", stream=stream)

    async def call_next(_request):
        return _Resp(200)

    resp = _run(_obs_middleware(_Req("POST", "/api/chat"), call_next))
    recs = [json.loads(ln) for ln in stream.getvalue().splitlines() if ln.strip()]
    access = [r for r in recs if r["logger"] == "agent.access"]
    assert access, f"未产出访问日志：{recs}"
    assert access[0]["rid"] == resp.headers["X-Request-Id"]
    assert "/api/chat" in access[0]["msg"] and "200" in access[0]["msg"]


def test_middleware_gives_each_request_a_distinct_id():
    from agent.run.chat import _obs_middleware

    async def call_next(_request):
        return _Resp(200)

    ids = [_run(_obs_middleware(_Req(), call_next)).headers["X-Request-Id"]
           for _ in range(3)]
    assert len(set(ids)) == 3, f"请求 id 重复：{ids}"


def test_middleware_counts_requests_and_failures():
    """计数口径：请求数 + 失败数（5xx 与抛出都算，且**不重复计数**）。"""
    from agent.run.chat import _obs_middleware

    before = counters_snapshot()

    async def ok(_request):
        return _Resp(200)

    async def boom(_request):
        return _Resp(500)

    _run(_obs_middleware(_Req(), ok))
    _run(_obs_middleware(_Req(), boom))

    def delta(name):
        return counters_snapshot().get(name, 0) - before.get(name, 0)

    assert delta("agent_requests_total") == 2, delta("agent_requests_total")
    assert delta("agent_request_errors_total") == 1, delta("agent_request_errors_total")

    async def raise_(_request):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _run(_obs_middleware(_Req(), raise_))
    assert delta("agent_request_errors_total") == 2, "抛出的异常未计入失败数"


# ---------------------------------------------------------------------------
# ③ 账本：落三列 + 可按 id 串联
# ---------------------------------------------------------------------------

def test_durable_records_trace_columns(tmp_path):
    """一次请求的关联标识随每条 durable 事件落库（三列齐全）。"""
    c = _container(tmp_path)

    async def s():
        await c.assemble()
        rid = bind_request_id("obs-durable")
        set_current_session_key("u:obs")
        set_current_run_id("u:obs:r1")
        await durable(c.root, "agent/test/one", {"n": 1})
        await durable(c.root, "agent/test/two", {"n": 2})
        rows = await c.root.db.query(
            c.root, "SELECT event, request_id, run_id, session FROM agent_events"
                    " WHERE event LIKE 'agent/test/%' ORDER BY seq")
        assert len(rows) == 2
        for row in rows:
            assert row["request_id"] == rid, row
            assert row["run_id"] == "u:obs:r1" and row["session"] == "u:obs", row
        await c.teardown()

    _run(s())


def test_trace_reassembled_by_request_id(tmp_path):
    """★ 核心用例：按同一 id 能把那一轮的事件全部拉出、按 seq 有序。

    这就是路线图 #5「可排查、可串联」的落点。修前 ``agent_events`` 无 id 列，
    只能全表扫 + 人工在 payload 里翻。查询语句与 ``/api/events`` 端点一致；
    端点本身由 ``.workbuddy/obs_live_check.py`` 在真实服务上端到端验证。
    """
    c = _container(tmp_path)

    async def s():
        await c.assemble()
        for i in range(5):
            await durable(c.root, f"agent/test/step{i}", {"i": i})
        bind_request_id("obs-other")            # 换绑 ⇒ 后续事件属"另一个请求"
        await durable(c.root, "agent/test/other", {"i": -1})

        rows = await c.root.db.query(
            c.root, "SELECT seq, event, request_id FROM agent_events"
                    " WHERE request_id=? ORDER BY seq", ("obs-trace",))
        assert len(rows) == 5, f"按 id 只查到 {len(rows)} 条（应恰好是本请求的 5 条）"
        assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)
        assert {r["event"] for r in rows} == {f"agent/test/step{i}" for i in range(5)}
        await c.teardown()

    bind_request_id("obs-trace")     # asyncio.run 起的新 task 会复制当前上下文 ⇒ 绑定生效
    _run(s())


def test_unbound_request_id_stays_empty_not_faked(tmp_path):
    """未绑定请求作用域时三列写**空串**——不编造 id（否则会把不同请求错并到一条链）。"""
    c = _container(tmp_path, "unbound.db")

    async def s():
        await c.assemble()
        await durable(c.root, "agent/test/unbound", {"ok": True})
        row = (await c.root.db.query(
            c.root, "SELECT request_id, run_id, session FROM agent_events"
                    " WHERE event='agent/test/unbound'"))[0]
        assert row == {"request_id": "", "run_id": "", "session": ""}, row
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# ④ 存量库：容错补列（钉住"服务起不来"那个坑）
# ---------------------------------------------------------------------------

def _make_legacy_db(path) -> None:
    """造一个**旧形状**的库：``agent_events`` 只有 4 列，且所有迁移已登记为已应用
    （⇒ ``MigrationRegistry.apply`` 全部跳过——正是在这种库上最容易被"迁移式 ALTER"炸掉）。"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS agent_events ("
                 " seq INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT, payload TEXT, at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS agent_migrations(name TEXT PRIMARY KEY,"
                 " applied_at TEXT)")
    conn.executemany("INSERT OR IGNORE INTO agent_migrations(name, applied_at) VALUES (?,?)",
                     [(name, "2026-01-01T00:00:00") for name, _, _ in MIGRATIONS])
    conn.commit()
    conn.close()


def test_legacy_events_table_is_upgraded_without_fatal(tmp_path):
    """★ 存量库（无三列）启动必须**成功**，且事后列与索引齐全、durable 立即可用。

    若把补列写成"迁移式 ALTER"：列一旦已存在，``ALTER`` 报 duplicate column ⇒
    ``MigrationRegistry`` 整组回滚 ⇒ 容器装配失败 ⇒ **服务起不来**。
    """
    db = tmp_path / "legacy.db"
    _make_legacy_db(str(db))
    c = _container(tmp_path, "legacy.db")

    async def s():
        await c.assemble()                      # 旧实现下这里会抛 ⇒ 装配失败
        cols = [r["name"] for r in await c.root.db.query(
            c.root, "PRAGMA table_info(agent_events)")]
        assert {"request_id", "run_id", "session"} <= set(cols), f"补列未生效：{cols}"
        idx = [r["name"] for r in await c.root.db.query(
            c.root, "SELECT name FROM sqlite_master WHERE type='index'"
                    " AND tbl_name='agent_events'")]
        assert "idx_agent_events_request" in idx, f"缺 request_id 索引：{idx}"
        bind_request_id("legacy-probe")
        await durable(c.root, "agent/test/legacy", {"ok": True})
        row = (await c.root.db.query(
            c.root, "SELECT request_id FROM agent_events"
                    " WHERE event='agent/test/legacy'"))[0]
        assert row["request_id"] == "legacy-probe"
        await c.teardown()

    _run(s())


def test_fresh_db_has_columns_from_ddl(tmp_path):
    """新建库由 DDL 直接带上三列（不依赖补列路径）。"""
    c = _container(tmp_path, "fresh.db")

    async def s():
        await c.assemble()
        cols = [r["name"] for r in await c.root.db.query(
            c.root, "PRAGMA table_info(agent_events)")]
        assert {"request_id", "run_id", "session"} <= set(cols), cols
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# ⑤ 结构化日志
# ---------------------------------------------------------------------------

@pytest.fixture
def root_logger_sandbox():
    """保存/还原 root 的 handler/级别/record factory（configure_logging 改的是全局状态）。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_factory = logging.getLogRecordFactory()
    try:
        yield root
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging.setLogRecordFactory(saved_factory)


def test_log_record_carries_trace_fields(root_logger_sandbox):
    """每条日志自动带 rid/session/run —— **现有调用点零改动**即可获得关联性。"""
    stream = io.StringIO()
    configure_logging(level="INFO", fmt="text", stream=stream)
    set_current_session_key("u:log")
    set_current_run_id("u:log:r1")
    rid = bind_request_id("4711-abcd")
    logging.getLogger("agent.test").info("hello trace")

    out = stream.getvalue()
    assert "hello trace" in out
    assert f"rid={rid}" in out, out
    assert "session=u:log" in out and "run=u:log:r1" in out, out


def test_json_log_format_is_parsable(root_logger_sandbox):
    """``--log-format json`` 输出单行 JSON（采集侧可直接解析），字段齐全。"""
    stream = io.StringIO()
    configure_logging(level="INFO", fmt="json", stream=stream)
    rid = bind_request_id("4711-json")
    logging.getLogger("agent.test").warning("json line")

    rec = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert rec["msg"] == "json line" and rec["level"] == "WARNING"
    assert rec["rid"] == rid and "session" in rec and "run" in rec


def test_configure_logging_is_idempotent_and_honours_stream(root_logger_sandbox):
    """重复调用不叠加 handler，且显式传入的 ``stream`` 生效（否则无法把日志重定向）。"""
    configure_logging(level="INFO", fmt="text", stream=io.StringIO())
    n1 = len(logging.getLogger().handlers)
    stream2 = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=stream2)
    n2 = len(logging.getLogger().handlers)
    assert n1 == n2, f"handler 被叠加：{n1} → {n2}"
    assert logging.getLogger().level == logging.DEBUG
    bind_request_id("4711-redir")
    logging.getLogger("agent.test").info("redirected")
    assert "redirected" in stream2.getvalue(), "重复调用时未改写输出流"


def test_counters_snapshot_is_a_copy():
    """快照返回副本（外部改写不得污染内部计数）。"""
    snap = counters_snapshot()
    snap["agent_bogus_total"] = 1
    assert "agent_bogus_total" not in counters_snapshot()


# ---------------------------------------------------------------------------
# ⑥ 机制守卫（源码级）
# ---------------------------------------------------------------------------

def test_agent_events_has_single_writer():
    """``agent_events`` 只允许 ``core/audit.py::durable`` 写入。

    旁路写入会**丢掉 request_id/run_id/session** ⇒ 那些行永远串不进 trace，
    且这种退化不会让任何行为测试变红，故用守卫钉死。
    """
    offenders: list[str] = []
    for sub in ("core", "sys", "plugins", "run"):
        for path in sorted((AGENT_DIR / sub).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for i, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if "INSERT INTO agent_events" in line:
                    rel = path.relative_to(AGENT_DIR).as_posix()
                    if rel != "core/audit.py":
                        offenders.append(f"{rel}:{i}")
    assert not offenders, f"账本出现旁路写入点（关联标识会被丢掉）：{offenders}"


def test_durable_insert_declares_trace_columns():
    """durable 的 INSERT 必须显式写出三列（防止有人只改 DDL 忘了写入侧）。"""
    src = (AGENT_DIR / "core" / "audit.py").read_text(encoding="utf-8")
    insert = src[src.index("INSERT INTO agent_events"):]
    insert = insert[:insert.index("VALUES")]
    for col in ("request_id", "run_id", "session"):
        assert col in insert, f"INSERT 未声明 {col} 列"


def test_obs_wiring_present_in_run_layer():
    """装配侧不许"忘了挂"：中间件注册、metrics 渲染计数器、events 支持按 id 过滤。"""
    src = (AGENT_DIR / "run" / "chat.py").read_text(encoding="utf-8")
    assert 'app.middleware("http")(_obs_middleware)' in src, "可观测性中间件未注册"
    assert "counters_snapshot()" in src, "/metrics 未渲染进程计数器"
    assert "WHERE request_id=?" in src, "/api/events 未支持按 request_id 过滤"
    assert "X-Request-Id" in src, "响应未回带 X-Request-Id"


def test_state_keys_not_polluted():
    """三列是**账本列**而非 ``ctx.state`` 键——不得混进 StateKeys（那是另一套契约）。"""
    assert not hasattr(StateKeys, "REQUEST_ID")
