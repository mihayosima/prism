"""agent.core.mcp_client —— 通用 MCP stdio 客户端（子进程 JSON-RPC，仅 stdlib）。

把一个 MCP 服务器（stdio 子进程）封装成可用的工具目录 + 调用能力：
    start() → initialize + notifications/initialized → list_tools() → call_tool(name, args)
进程断连/超时 → 诚实抛错（不静默）。零第三方依赖、零业务词。

S4 加固（死亡可见 + 归因唯一 + 不泄漏子孙进程）：
- ``_wait_loop`` 常驻 ``await proc.wait()``，子进程一退出即记录**退出码**并置 ``_dead``
  ⇒ 客户端从此**知道自己死了**（此前 ``returncode`` 恒为 None，客户端对死亡毫无概念）；
- ``_request`` 开头检查 ``_dead``，报错**恒定**为带退出码的 ``ConnectionError``
  ⇒ 消除「首次 EOF、之后 ConnectionResetError」的漂移（后者只是往死管道写入的副作用）；
- ``close()`` 先置 ``_closing`` 并立即以「已关闭」失败在途请求，**不再**被 ``_read_loop``
  的 finally 抢先误报成「子进程已退出」（归因错位的根因）；
- 子进程以**独立进程组**启动，退出时**杀整棵进程树**（Windows ``taskkill /T`` / POSIX
  ``killpg``）⇒ 不再残留 uvx→uv→python 这类孙进程（实测线上曾堆积 6 天）。

S5 加固（宿主安全删除策略隔离）：
- MCP 服务器是**独立第三方程序**，不应继承宿主（WorkBuddy CLI）注入到本进程树里的
  文件操作策略。宿主通过 ``PYTHONPATH=<...>\\cli\\vendor\\shim`` 让每个 Python 子进程自动
  加载 ``sitecustomize.py``，其中 ``_check_bulk_delete_guard`` 在计数超阈值时会
  ``raise SystemExit(1)``；``SystemExit`` 是 BaseException，会顺着 anyio TaskGroup /
  ``stdio_server()`` 把**整个 MCP 服务器进程**打掉（实测：一次 ``delete_path`` 让
  windows_mcp 退出码 1，之后列目录/跑命令全部 EOF）。
- 且该守卫的计数按 ``CODEBUDDY_CONVERSATION_REQUEST_ID`` 归档，而它是**启动时冻结**的：
  真正的 CLI 每个工具调用都会刷新它（计数=本 turn），但本 harness 后端是长驻进程，
  其后代共享同一个 id ⇒ 计数变成「进程生命周期累计器」，必然越过阈值并**永久**卡死。
- 对策：spawn 时剥掉 4 个守卫相关变量，**保留 PYTHONPATH**。
  ``sitecustomize.py::_check_bulk_delete_guard`` 的首个分支即
  ``if not STATE_DIR or not TOOL_CALL_ID: return`` ⇒ 守卫直接短路（不再 SystemExit），
  而 shim 的「unlink → 移入回收站」语义仍在 ⇒ 恢复删除能力且不丢安全网，
  同时**不改动宿主任何状态**。

S6 加固（参数 schema 保真，P2）：
- ``build_task_schema`` 此前只抄属性的 ``type`` 再压成内部名，MCP 侧的类型信息被
  **大面积压扁**：``mode`` 的 8 个合法取值（``read|write|copy|move|delete|list|search|info``，
  躺在 ``enum`` 里）整条丢失，``destination`` / ``recursive`` / ``limit`` 等
  ``anyOf: [{type: string}, {type: null}]`` 的联合类型统一退化成 ``object``——
  模型既不知道参数叫什么（实测把 ``mode`` 猜成 ``action``），也不知道该填什么值。
- 对策：``enum`` / ``anyOf`` / ``default`` / 属性 ``description`` **原样保留**进
  内核 task schema；``request_cost.wire_tools`` 再把它们还原成 JSON Schema 片段发给模型。
  校验语义（``type`` / ``required``）保持原样，只是多带元数据。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
from typing import Any

_MCP_VERSION = "2024-11-05"

#: 宿主（WorkBuddy CLI）注入的「安全删除」策略变量。MCP 子进程是独立第三方程序，
#: 不应继承宿主的文件操作策略；尤其批量删除守卫会 ``raise SystemExit(1)``，
#: 顺着 asyncio 任务组把整个 MCP 服务器进程打死（详见模块 docstring 的 S5 段）。
#: 只剥这 4 个：``_check_bulk_delete_guard`` 首个分支即 ``if not STATE_DIR or not
#: TOOL_CALL_ID: return``，剥掉即短路守卫，而 PYTHONPATH 保留 ⇒ 回收站语义不丢。
_HOST_SAFE_DELETE_ENV = (
    "CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR",
    "CODEBUDDY_TOOL_CALL_ID",
    "CODEBUDDY_SESSION_ID",
    "CODEBUDDY_CONVERSATION_REQUEST_ID",
)


def child_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """构造 MCP 子进程环境：继承本进程环境，但剥掉宿主的安全删除守卫变量。

    在合并 ``env`` **之后**再剥，保证「MCP 子进程永远拿不到这些变量」这一不变量成立。
    """
    merged = {**os.environ, **(env or {})}
    for key in _HOST_SAFE_DELETE_ENV:
        merged.pop(key, None)
    return merged


logger = logging.getLogger("agent.mcp")

#: JSON Schema 原生类型 → 内核轻量校验的类型名（``tasks.validate_kwargs`` 认识的那几种）。
_JSON_TO_TYPE = {"string": "str", "number": "float", "integer": "int",
                 "boolean": "bool", "array": "array", "object": "object"}


def _json_type(prop: dict) -> str:
    """从属性片段推断 JSON 原生类型名（**只用于校验**，宁宽松不误伤）。

    优先取 ``type``；没有则看 ``anyOf``：全分支同构（除 ``null``）取该型，
    异构或空则退 ``object``——``object`` 的校验器恒真，不会把合法调用拦下来。
    """
    t = prop.get("type")
    if isinstance(t, str):
        return t
    named = {b.get("type") for b in (prop.get("anyOf") or [])
             if isinstance(b, dict) and isinstance(b.get("type"), str) and b["type"] != "null"}
    return named.pop() if len(named) == 1 else "object"


def build_task_schema(input_schema: dict | None) -> dict[str, dict]:
    """MCP ``inputSchema`` → 内核 task schema（``{参数名: {type, required, ...}}``）。

    P2：除 ``type`` / ``required`` 外，**保真**带上 ``enum`` / ``anyOf`` / ``default`` /
    属性 ``description``。这些键对 ``validate_kwargs`` 是透明的（它只读 ``type`` /
    ``required`` / ``default``），但 ``request_cost.wire_tools`` 会把它们还原成
    JSON Schema 交给模型 —— 这正是模型此前看不到 ``mode`` 合法取值的原因。

    注意 ``default`` 的副作用：``validate_kwargs`` 会为**缺省的选填参数**填上默认值，
    即 MCP server 将显式收到它自己声明的默认值（语义等价，且让调用更可预测）。
    """
    schema = input_schema or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    out: dict[str, dict] = {}
    for name, raw in props.items():
        prop = raw if isinstance(raw, dict) else {}
        spec: dict[str, Any] = {"type": _JSON_TO_TYPE.get(_json_type(prop), "object"),
                                "required": name in required}
        if isinstance(prop.get("enum"), list):
            spec["enum"] = list(prop["enum"])
        branches = [b["type"] for b in (prop.get("anyOf") or [])
                    if isinstance(b, dict) and isinstance(b.get("type"), str)]
        if branches:
            # 联合类型（**JSON 原生类型名**，如 ["boolean","string"]）：发 wire 时优先于
            # 上面归一化出的 type —— 同时发两者会自相矛盾。
            spec["anyOf"] = branches
        if "default" in prop:
            spec["default"] = prop["default"]
        if prop.get("description"):
            spec["description"] = str(prop["description"])
        out[name] = spec
    return out


class McpStdioClient:
    """单个 MCP 服务器子进程的 stdio 客户端。

    S2/S3 加固：
    - stderr 由独立任务持续消费（默认降级为 debug 日志，可传 ``on_stderr`` 回调），
      话痨子进程不会再把 OS 管道缓冲区写满而整体卡死；
    - 子进程退出（stdout EOF）或 ``close()`` 时，在途 ``_pending`` 请求**立即**
      收到 ``ConnectionError``，而非悬挂到超时；
    - 请求方取消 / 超时后，future 从 ``_pending`` 移除（迟到的响应不再泄漏）。

    S4 加固：见模块 docstring（``is_alive`` / ``dead_reason`` / 归因唯一 / 杀进程树）。
    """

    def __init__(self, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None, timeout_s: float = 120.0,
                 on_stderr=None):
        self._cmd = command
        self._args = list(args or [])
        self._env = env or {}
        self._timeout = timeout_s
        self._on_stderr = on_stderr
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._waiter: asyncio.Task | None = None
        self._rid = 0
        self._pending: dict[int, asyncio.Future] = {}
        # S4：死亡状态（None = 未死/未启动；字符串 = 死因，含退出码）
        self._dead: str | None = None
        self._closing = False

    # ---------- 公开状态（S4） ----------

    @property
    def is_alive(self) -> bool:
        """子进程是否仍可用（供上层探活 / 决定是否重启）。"""
        return self._proc is not None and self._dead is None

    @property
    def dead_reason(self) -> str | None:
        """死因（含退出码）；存活或未启动时为 None。"""
        return self._dead

    async def start(self) -> None:
        full_env = child_env(self._env)
        self._dead = None
        self._closing = False
        # 独立进程组（Windows: CREATE_NEW_PROCESS_GROUP / POSIX: start_new_session）
        # ⇒ 退出时可整棵杀死，不留 uvx→uv→python 之类的孙进程。
        extra: dict[str, Any] = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                                 if os.name == "nt" else {"start_new_session": True})
        self._proc = await asyncio.create_subprocess_exec(
            self._cmd, *self._args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=full_env, **extra)
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        self._waiter = asyncio.create_task(self._wait_loop())
        try:
            await self._request("initialize", {
                "protocolVersion": _MCP_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agent", "version": "0.1.0"},
            })
            self._notify("notifications/initialized", {})
        except BaseException:
            # 半途失败（超时/子进程崩/被取消）⇒ 收拾干净，不留孤儿进程
            await self.close()
            raise

    async def list_tools(self) -> list[dict]:
        result = await self._request("tools/list", {})
        return list(result.get("tools") or [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        content = list(result.get("content") or [])
        text = "\n".join(str(c.get("text") or "") for c in content if c.get("type") == "text")
        if result.get("isError"):
            raise RuntimeError(text or f"工具 {name!r} 返回错误")
        return {"content": content, "text": text,
                "structured": result.get("structuredContent")}

    async def close(self) -> None:
        """关闭并回收子进程（幂等）。

        S4 顺序修正：**先**置 ``_closing`` 并立即以「已关闭」失败在途请求，再 cancel 读循环
        —— 否则 ``_read_loop`` 的 finally 会抢先把在途请求报成「子进程已退出」，
        把**主动关闭**误传成**崩溃**（归因错位）。
        """
        self._closing = True
        self._fail_pending("MCP 客户端已关闭")
        tasks = (self._reader, self._stderr_task, self._waiter)
        for t in tasks:
            if t is not None:
                t.cancel()
        for t in tasks:
            if t is None:
                continue
            try:
                await t
            except asyncio.CancelledError:
                if not t.cancelled():
                    raise      # 本协程自身被取消 ⇒ 保持取消语义（finally 收进程仍会执行）
            except Exception:  # noqa: BLE001 —— 收尾异常不影响关闭
                pass
        self._reader = self._stderr_task = self._waiter = None
        proc = self._proc
        self._proc = None
        if self._dead is None:
            self._dead = "MCP 客户端已关闭"
        if proc is not None and proc.returncode is None:
            await self._kill_tree(proc)

    async def _kill_tree(self, proc) -> None:
        """终止子进程**及其后代**。

        必要性见模块 docstring：``uvx → uv → python`` 这类启动链下，只 terminate 直接子进程
        会让真正的 server 变成孤儿并长期驻留（线上实测见到跑了 6 天的实例）。
        """
        pid = proc.pid
        try:
            if proc.stdin is not None:
                proc.stdin.close()      # 先关 stdin：让仍活着的 server 有机会自行退出
        except Exception:  # noqa: BLE001
            pass
        if os.name == "nt":
            try:
                k = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(k.wait(), timeout=5.0)
            except Exception:  # noqa: BLE001 —— 回退为单进程终止
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # ---------- 内部 ----------

    def _fail_pending(self, reason: str) -> None:
        """在途请求立即报错（子进程退出 / 客户端关闭），不留悬挂 future。"""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self._pending.clear()

    def _mark_dead(self, reason: str, *, authoritative: bool = False) -> None:
        """记录死因并让在途请求立即失败。

        ``authoritative=True``（由 ``_wait_loop`` 带**退出码**上报）可覆盖此前的非权威死因
        —— 两条通路会竞争：stdout 先 EOF、随后 ``proc.wait()`` 才返回。退出码信息量更大，
        故允许它后来居上，保证最终 ``dead_reason`` 是「exit code N」这类可定位的表述。

        S4：死因**上日志**——此前 stderr 默认走 debug、插件的运行期异常又只抛给调用方，
        导致「子进程为什么死」在服务端日志里毫无痕迹。
        """
        if self._dead is None or (authoritative and "exit code" not in self._dead):
            self._dead = reason
            logger.warning("%s", reason)
        self._fail_pending(self._dead)

    async def _wait_loop(self) -> None:
        """常驻等待子进程退出 ⇒ 拿到**退出码**并置死。

        S4 的核心：此前没人 ``await proc.wait()``，``returncode`` 恒为 None，
        客户端对"子进程已死"毫无感知 ⇒ 死亡后仍按存活处理。
        """
        proc = self._proc
        if proc is None:
            return
        try:
            rc = await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 等待失败不额外抛
            return
        if not self._closing:
            self._mark_dead(f"MCP 子进程已退出（exit code {rc}）", authoritative=True)

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except Exception:  # noqa: BLE001 —— 非 JSON 行忽略
                    continue
                mid = msg.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if "error" in msg:
                        fut.set_exception(RuntimeError(json.dumps(msg["error"], ensure_ascii=False)))
                    else:
                        fut.set_result(msg.get("result"))
        finally:
            if self._closing:
                # 关闭路径：归因必须是「已关闭」，绝不能报成「子进程已退出」
                self._fail_pending("MCP 客户端已关闭")
            else:
                # stdout EOF 通常与进程退出同时发生 ⇒ 先给 _wait_loop 一个机会取到退出码，
                # 使死因尽量是「exit code N」这种可定位的表述，而非笼统的「stdout 已关闭」。
                w = self._waiter
                if w is not None and not w.done() and w is not asyncio.current_task():
                    try:
                        await asyncio.wait_for(asyncio.shield(w), timeout=1.0)
                    except Exception:  # noqa: BLE001 —— 取不到就回退到笼统表述
                        pass
                self._mark_dead("MCP 子进程 stdout 已关闭（不再有响应）")

    async def _stderr_loop(self) -> None:
        """持续消费 stderr（S2）：防止话痨子进程写满管道缓冲区而卡死。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if self._on_stderr is not None:
                try:
                    self._on_stderr(text)
                except Exception:  # noqa: BLE001 —— 回调异常绝不影响主链路
                    pass
            else:
                logger.debug("mcp stderr: %s", text)

    async def _request(self, method: str, params: dict) -> Any:
        if self._proc is None or self._proc.stdin is None:
            raise ConnectionError("MCP 客户端未启动或已关闭")
        if self._dead is not None:
            # S4：死亡后**恒定**报同一条带退出码的错误，不再漂移成 ConnectionResetError
            #（那只是往已断管道写入的副作用，对定位毫无价值且会误导排查）。
            raise ConnectionError(self._dead)
        self._rid += 1
        rid = self._rid
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            payload = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            self._proc.stdin.write((payload + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)       # 迟到的响应不得悬挂在表里
            raise
        except asyncio.CancelledError:
            self._pending.pop(rid, None)
            raise

    def _notify(self, method: str, params: dict) -> None:
        if self._proc is not None and self._proc.stdin is not None and self._dead is None:
            payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
            self._proc.stdin.write((payload + "\n").encode("utf-8"))


__all__ = ["McpStdioClient", "build_task_schema", "child_env"]