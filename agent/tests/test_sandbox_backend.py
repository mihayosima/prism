"""P0 分级沙箱后端骨架（core/sandbox_backend）单测。

纪律：本文件只测**纯逻辑**（dataclass / 后端选择 / 降级语义），不依赖容器装配，
与 ``test_sandbox_plugin.py`` 的插件级测试互补。

★ 两条**契约守卫**是本文件最重要的用例（由代码复核得出，防止后来者改崩桥）：
  1. ``SpawnOptions.to_dict()`` **必须**含 ``cwd`` / ``env`` —— 既有 MCP 桥
     （playwright / windows）只读这两个键（``plugins/playwright_mcp/__init__.py:99-104``）。
  2. 接缝 ``Services.CHILD_SPAWN_OPTIONS`` 的 callable 只被**单参**调用
     （``fn(current_session_key(ctx))``）⇒ 后端不能要求 ``(target, policy, session_key)``
     三参形状。本文件用"桥的调用方式"直接驱动断言，契约一改这里就红。
"""

from __future__ import annotations

import os

import pytest

from agent.core.sandbox_backend import (BackendUnavailable, ContainerBackend,
                                        DEFAULT_CONTAINER_IMAGE, NullBackend,
                                        SpawnOptions, Wsl2Backend, apply_wrap,
                                        call_spawn_provider, describe_backend,
                                        is_portable, runnable_in, select_backend,
                                        to_wsl_path, unportable_bridges)
from agent.core.sandbox_backend import _AUTO_CANDIDATES, _map_path, _probe


def _bridge_style_read(opts: dict | None):
    """**模拟桥的消费方式**：只传一个 session_key，且只读 ``cwd`` / ``env`` 两键。

    这段是照 ``plugins/playwright_mcp/__init__.py:97-104`` 抄的调用姿势，
    用来锁死接缝形状 —— 改契约 ⇒ 这里先红。
    """
    if not opts:
        return None, {}
    return opts["cwd"], opts["env"]


# ---------------------------------------------------------------------------
# SpawnOptions：形状与向后兼容
# ---------------------------------------------------------------------------

def test_to_dict_keeps_legacy_keys_bridges_depend_on():
    """★ 契约守卫①：``cwd`` / ``env`` 必须在，否则既有桥 KeyError。"""
    d = SpawnOptions(cwd="data/sandbox/s1", env={"TMP": "data/sandbox/s1"}).to_dict()
    assert "cwd" in d and "env" in d
    cwd, env = _bridge_style_read(d)
    assert cwd == "data/sandbox/s1" and env["TMP"] == "data/sandbox/s1"


def test_to_dict_omits_empty_optional_fields():
    """可选字段为空时不入字典 —— 不把一堆 None 塞给今天不消费它们的桥。"""
    d = SpawnOptions(cwd="x", env={}).to_dict()
    assert "token" not in d and "creationflags" not in d and "proxy_addr" not in d
    assert "netns" not in d and "seccomp_fd" not in d


def test_to_dict_carries_enforcement_fields_when_set():
    """后端真落地后（P1/P4）的字段能被带出去，供 mcp_client 消费。"""
    d = SpawnOptions(cwd="x", env={}, token=123, creationflags=0x00000200,
                     net_policy="proxy", proxy_addr="127.0.0.1:8899",
                     netns=True).to_dict()
    assert d["token"] == 123 and d["creationflags"] == 0x00000200
    assert d["net_policy"] == "proxy" and d["proxy_addr"] == "127.0.0.1:8899"
    assert d["netns"] is True


# ---------------------------------------------------------------------------
# NullBackend：诚实 none
# ---------------------------------------------------------------------------

def test_null_backend_is_honestly_none():
    b = NullBackend()
    assert b.name == "null" and b.strength == "none"
    assert b.capability_probe()["available"] is True
    # 只有"本就没声明隔离"才算可兑现
    assert b.can_enforce("none") is True
    assert b.can_enforce("read-only") is False
    assert b.can_enforce("workspace-write") is False


def test_null_backend_keeps_workspace_and_marks_no_enforcement():
    """落点收拢**保留**（不劣化），但强制信息如实为 none。"""
    base = SpawnOptions(cwd="data/sandbox/s1", env={"TMP": "data/sandbox/s1"},
                        mode="workspace-write")
    out = NullBackend().spawn_options(base, {"network": "allow"}, "s1")
    assert out.cwd == "data/sandbox/s1"                 # 落点没被吃掉
    assert out.env == {"TMP": "data/sandbox/s1"}
    assert out.strength == "none" and out.enforced_by == "none"
    assert out.backend == "null"


def test_null_backend_picks_up_network_policy():
    out = NullBackend().spawn_options(SpawnOptions(), {"network": "proxy"})
    assert out.net_policy == "proxy"


# ---------------------------------------------------------------------------
# select_backend：降级语义（auto 不报错 / 显式不可得可见 / strict 抛错）
# ---------------------------------------------------------------------------

def test_off_and_null_are_first_class_tiers():
    """off/null 是**一等档位**：不算降级、不算错误。"""
    for name in ("off", "null"):
        backend, info = select_backend(name)
        assert backend.name == "null"
        assert info["fell_back"] is False and info["error"] is False


def test_auto_selects_available_or_off_without_error():
    """★ 用户裁定：进程级运行时不满足 ⇒ off，**不报错**；满足则选中它。

    P4/P5 后 wsl2 与 container 都已实现：本机有 WSL2+bwrap ⇒ auto 选中 wsl2；
    否则有 Docker ⇒ 选中 container（"能用就用"的语义，v1.4 裁定 auto 顺序
    wsl2 → container）；都不可得 ⇒ 回落 null 且**不得**报 error。跨环境稳定。
    """
    backend, info = select_backend("auto")
    expected = next((c for c in _AUTO_CANDIDATES
                     if _probe(c).get("available")), "null")
    assert backend.name == expected and info["backend"] == expected
    assert info["error"] is False and info["fell_back"] is False


def test_explicit_unavailable_backend_is_visible_not_silent():
    """显式点名却要不到 ⇒ **可见**（fell_back + error + enforced_by=backend-error）。

    用 ``win-token`` 作样例：P1 spike 实测恒不可用（0xC0000142），**不依赖本机
    有没有 WSL/Docker**（P5 后 container 在本机可用，不能再当"恒不可得"样例）。
    """
    backend, info = select_backend("win-token")
    assert backend.name == "null"                 # 回落
    assert info["fell_back"] is True and info["error"] is True
    assert info["enforced_by"] == "backend-error"


def test_unknown_backend_name_falls_back_to_auto_semantics():
    backend, info = select_backend("乱写的后端")
    assert backend.name in ("wsl2", "container", "null") and info["error"] is False


def test_strict_raises_when_explicit_backend_unavailable():
    """strict=true：显式要求不可得 ⇒ 抛（fail-closed 的**告警侧**）。

    用 win-token（spike 实测恒不可得）⇒ 不依赖本机 Docker/WSL  availability。
    """
    with pytest.raises(BackendUnavailable):
        select_backend("win-token", strict=True)


def test_strict_does_not_affect_auto():
    """auto 的本意就是"尽力给、没有就 off"⇒ strict 不该把 auto 变成硬失败。"""
    backend, info = select_backend("auto", strict=True)
    assert backend.name in ("wsl2", "container", "null") and info["error"] is False


def test_describe_backend_never_raises():
    """``status()`` / 审计路径用：即使 strict 也吞异常，但要留 error 标记。"""
    info = describe_backend("win-token", strict=True)
    assert info["error"] is True and info["enforced_by"] == "backend-error"
    assert describe_backend("auto")["error"] is False


# ---------------------------------------------------------------------------
# 与插件自述的衔接（describe 据后端真实强度，不再硬编码）
# ---------------------------------------------------------------------------

def test_plugin_describe_uses_backend_strength_when_given():
    from agent.plugins.sandbox import DEFAULT_CONFIG, describe

    # 无后端信息（纯函数穷举路径）⇒ 诚实 none，与旧硬编码天花板同值
    d = describe({**DEFAULT_CONFIG, "mode": "workspace-write"})
    assert d["enforcement"] == "none" and d["gap"] is True

    # 给了后端信息 ⇒ 由后端自报（这里用假后端证明"不再写死"）
    fake = {"backend": "wsl2", "strength": "partial", "enforced_by": "wsl2-landlock",
            "error": False, "reason": "已兑现"}
    d2 = describe({**DEFAULT_CONFIG, "mode": "workspace-write"}, "", fake)
    assert d2["enforcement"] == "partial" and d2["gap"] is False
    assert d2["backend"] == "wsl2" and d2["enforced_by"] == "wsl2-landlock"


def test_seam_contract_single_arg_callable():
    """★ 契约守卫②：接缝 callable 必须**能只用一个参数**被调用（桥就是这么调的）。"""
    def _provider(session_key: str = "") -> dict | None:
        # 这就是插件侧未来的形状：单参、返回 dict|None
        backend, _info = select_backend("auto")
        opts = backend.spawn_options(SpawnOptions(cwd="ws", env={}), {}, session_key)
        return opts.to_dict()

    out = _provider("sess-1")          # 桥只传 session_key
    cwd, env = _bridge_style_read(out)
    # cwd 必须原样可回读（桥只认这两个键）；env 恒为 dict（wsl2 会注入 PYTHONPATH）
    assert cwd == "ws" and isinstance(env, dict)


# --------------------------------------------------------------------------- #
# P4：WSL2 后端（bwrap + netns）
# --------------------------------------------------------------------------- #

_WSL_OK = {"available": True, "reason": "注入：wsl2 可用", "distro": "Ubuntu"}
_SKILLS_ARGS = ["-m", "agent.run.skills_mcp_adapter"]


def _wsl_opts(mode: str = "workspace-write", network: str = "deny",
              cwd: str = r"C:\repo\data\sandbox\s1") -> dict:
    be = Wsl2Backend(probe_result=_WSL_OK)
    opts = be.spawn_options(SpawnOptions(cwd=cwd, env={"SKILLS_DATA_DIR": r"C:\repo\data\skills"}),
                            {"mode": mode, "network": network}, "s1")
    return opts.to_dict()


def test_to_wsl_path():
    assert to_wsl_path(r"C:\repo\data") == "/mnt/c/repo/data"
    assert to_wsl_path("C:/repo/data") == "/mnt/c/repo/data"
    assert to_wsl_path("/already/posix") == "/already/posix"     # 非盘符原样


def test_wsl2_probe_injection_and_can_enforce():
    be = Wsl2Backend(probe_result=_WSL_OK)
    assert be.capability_probe()["available"] is True
    assert be.can_enforce("workspace-write") and be.can_enforce("read-only")
    assert not be.can_enforce("none")                            # 声称无隔离 ⇒ 不该选它
    assert be.strength == "partial"                              # 诚实：无 seccomp/Landlock
    assert be.describe()["gaps"]                                 # gap 必须可见


def test_wsl2_spawn_options_emits_wrap():
    d = _wsl_opts()
    assert d["backend"] == "wsl2" and d["enforced_by"] == "wsl2-bwrap"
    assert d["strength"] == "partial" and d["netns"] is True     # deny ⇒ 内核断网
    wrap = d["wrap"]
    assert wrap["mode"] == "shell"
    assert wrap["argv"][:3] == ["wsl.exe", "-e", "bash"]
    assert wrap["inner"].startswith("bwrap")                     # 实测可用的包裹命令
    assert "--unshare-net" in wrap["inner"]
    assert "--bind /mnt/c/repo/data/sandbox/s1" in wrap["inner"]  # workspace-write 可写
    assert "--ro-bind /mnt" in wrap["inner"]                     # 项目根只读


def test_wsl2_read_only_has_no_writable_bind():
    d = _wsl_opts(mode="read-only")
    assert "--bind /mnt" not in d["wrap"]["inner"]               # 无可写目录（仅 tmpfs）


def test_wsl2_allow_network_does_not_unshare_net():
    d = _wsl_opts(network="allow")
    assert not d.get("netns") and "--unshare-net" not in d["wrap"]["inner"]


def test_wsl2_env_paths_are_translated():
    """Windows 路径的环境变量在 WSL 内无效 ⇒ 必须映射（否则技能目录读不到）。"""
    d = _wsl_opts()
    assert d["env"]["SKILLS_DATA_DIR"] == "/mnt/c/repo/data/skills"


def test_wsl2_wrap_contract_keeps_cwd_env():
    """★ 契约守卫：套了包装也**不能丢** cwd/env —— 既有桥只读这两个键。"""
    d = _wsl_opts()
    cwd, env = _bridge_style_read(d)
    assert cwd == r"C:\repo\data\sandbox\s1"                     # 外层仍是 Windows 路径
    assert "SKILLS_DATA_DIR" in env


def test_apply_wrap_hits_translation():
    d = _wsl_opts()
    cmd, args, _env, _cwd, applied = apply_wrap(
        r"C:\py\python.exe", _SKILLS_ARGS, {"SKILLS_DATA_DIR": "/mnt/c/repo/data/skills"},
        r"C:\repo\data\sandbox\s1", d["wrap"])
    assert applied is True
    assert cmd == "wsl.exe"
    inner = args[-1]
    assert "python3 -m agent.run.skills_mcp_adapter" in inner    # 已翻译成 WSL 内命令
    assert "SKILLS_DATA_DIR=/mnt/c/repo/data/skills" in inner    # env 进 inner
    assert "bwrap --ro-bind" in inner                            # 隔离器在前（env 赋值在其**之前**）


def test_apply_wrap_miss_degrades_without_crash():
    """未命中翻译 ⇒ **原样启动**（无隔离），绝不硬套（否则 Windows exe 进 WSL 必崩）。"""
    d = _wsl_opts()
    cmd, args, _env, _cwd, applied = apply_wrap(
        r"C:\Tools\win-only.exe", ["--win-only"], {}, None, d["wrap"])
    assert applied is False
    assert cmd.endswith("win-only.exe") and args == ["--win-only"]


def test_apply_wrap_none_or_plain_is_passthrough():
    cmd, args, _env, _cwd, applied = apply_wrap("python", _SKILLS_ARGS, {}, None, None)
    assert (cmd, args, applied) == ("python", _SKILLS_ARGS, False)


def test_apply_wrap_quotes_spaces():
    """env 值进 inner 必须 **映射 + shlex 转义**（路径含空格/反斜杠时裸拼会炸）。

    ★ 映射也在 ``apply_wrap`` 里做：桥传来的 env 是 **Windows 路径**（后端映射的是另一份
    ``SpawnOptions.env``），若不在此翻译，子进程在 WSL 内拿到 ``C:\\...`` 会直接失效。
    """
    d = _wsl_opts()
    _c, args, _e, _w, applied = apply_wrap(
        "python.exe", _SKILLS_ARGS, {"A": r"C:\a b\c"}, None, d["wrap"])
    assert applied and "A='/mnt/c/a b/c'" in args[-1]           # 映射 + 保留空格


def test_win_token_is_verdict_unavailable():
    """P1 spike 结论：win-token 恒不可用且给出可追溯原因（不是"未实现"）。"""
    be, info = select_backend("win-token")
    assert be.name == "null" and info["error"] is True and info["fell_back"] is True
    assert "0xC0000142" in info["reason"]


def test_auto_selects_by_real_capability():
    """按**真实能力**断言：有 WSL2+bwrap ⇒ wsl2；否则有 Docker ⇒ container；
    都不可得 ⇒ 诚实回落 null。

    不做"本机一定有 WSL/Docker"的硬断言（CI/换机会红），只锁"不谎报"这一不变量。
    """
    probe = Wsl2Backend().capability_probe()          # 真实探测（带缓存，只跑一次）
    be, _info = select_backend("auto")
    expected = "wsl2" if probe.get("available") else "null"
    if expected == "null" and ContainerBackend().capability_probe().get("available"):
        expected = "container"
    assert be.name == expected


# --------------------------------------------------------------------------- #
# P8：目标可移植性（纯函数，不依赖本机有无 WSL/Docker）
# --------------------------------------------------------------------------- #

def test_portability_declaration():
    assert runnable_in("windows_mcp") == ("off",)
    assert "wsl2" in runnable_in("skills_mcp")
    # 未登记的桥按"可移植"处理 ⇒ 新桥不因漏登记被误降级（不劣化）
    assert "wsl2" in runnable_in("某个新桥")


def test_runnable_in_accepts_both_id_and_domain():
    """审计按工具名推域（``windows``），桥按 id 自称（``windows_mcp``）⇒ 两种都要认。

    ★ 只认一种会让 ``is_portable("windows", "wsl2")`` 落到默认值 True
    ⇒ 把**不可移植**目标误判为可移植（硬套包装 ⇒ 子进程必崩）。
    """
    assert runnable_in("windows") == runnable_in("windows_mcp") == ("off",)
    assert is_portable("windows", "wsl2") is False
    assert is_portable("windows_mcp", "wsl2") is False
    assert is_portable("windows_mcp", "container") is False


def test_off_is_always_portable():
    """不隔离 ⇒ 一定能跑。off/null 是**一等档位**，不是失败态。"""
    for b in ("windows_mcp", "skills_mcp", "unknown"):
        for backend in ("off", "null", ""):
            assert is_portable(b, backend) is True


def test_unportable_bridges_lists_only_blocked():
    assert unportable_bridges("wsl2") == ["windows_mcp"]
    assert unportable_bridges("off") == []
    assert unportable_bridges("null") == []


def test_call_spawn_provider_adapts_arity():
    """升级到双参 **不能** 打破单参 provider（历史/第三方可能只收 session_key）。"""
    seen = {}

    def one_arg(sk):
        seen["args"] = (sk,)
        return {"cwd": "ws"}

    def two_arg(sk, owner):
        seen["args"] = (sk, owner)
        return {"cwd": "ws", "owner": owner}

    assert call_spawn_provider(one_arg, "s1", "skills_mcp") == {"cwd": "ws"}
    assert seen["args"] == ("s1",)                       # 单参就单参调，不试错
    assert call_spawn_provider(two_arg, "s1", "windows_mcp")["owner"] == "windows_mcp"
    assert seen["args"] == ("s1", "windows_mcp")
    assert call_spawn_provider(None, "s1") is None       # 不可调用 ⇒ 安全返回


# --------------------------------------------------------------------------- #
# P5：容器级后端（Docker）
# --------------------------------------------------------------------------- #

_DOCKER_OK = {"available": True, "reason": "注入：Docker 可用", "version": "29.4.0"}


def _docker_opts(mode: str = "workspace-write", network: str = "deny") -> dict:
    # ★ 路径必须落在**真实项目根**（``os.getcwd()``）下：``ContainerBackend`` 的
    # ``path_map`` 以 ``os.getcwd()`` 为前缀，用虚构的 ``C:/repo`` 会映射不命中
    # （那正是 ``test_apply_wrap_argv_mode_order`` 曾经红掉的原因）。
    repo = os.getcwd()
    be = ContainerBackend(probe_result=_DOCKER_OK)
    opts = be.spawn_options(
        SpawnOptions(cwd=os.path.join(repo, "data", "sandbox", "s1"),
                     env={"SKILLS_DATA_DIR": os.path.join(repo, "data")}),
        {"mode": mode, "network": network}, "s1")
    return opts.to_dict()


def test_container_hardening_flags():
    d = _docker_opts()
    argv = d["wrap"]["argv"]
    joined = " ".join(argv)
    assert d["backend"] == "container" and d["strength"] == "full"
    assert d["enforced_by"] == "docker-container"
    assert "-i" in argv and "--rm" in argv                  # stdio 保活 + 不留残容器
    assert "--cap-drop ALL" in joined and "--security-opt no-new-privileges" in joined
    assert "--network none" in joined                       # deny ⇒ 内核级无网
    assert "--pids-limit" in joined


def test_container_mounts_are_read_only_for_repo():
    d = _docker_opts()
    joined = " ".join(d["wrap"]["argv"])
    assert ":ro" in joined and "/repo" in joined            # 项目根只读挂载
    assert "/workspace:rw" in joined                        # 工作目录可写


def test_container_read_only_has_no_writable_mount():
    d = _docker_opts(mode="read-only")
    joined = " ".join(d["wrap"]["argv"])
    assert "--read-only" in joined
    assert "/workspace:rw" not in joined


def test_container_allow_network_keeps_default_net():
    d = _docker_opts(network="allow")
    assert "--network none" not in " ".join(d["wrap"]["argv"])


def test_container_wrap_is_argv_mode_with_image():
    w = _docker_opts()["wrap"]
    assert w["mode"] == "argv" and w["image"] == DEFAULT_CONTAINER_IMAGE
    assert w["path_map"] and w["path_map"][0][1] == "/repo"


def test_apply_wrap_argv_mode_order():
    """★ 顺序铁律：``-e`` 必须在 **image 之前**，命令在 image 之后。

    反了会被 docker 当成"要在容器内执行的命令"，桥直接起不来。
    """
    d = _docker_opts()
    cmd, args, _env, _cwd, applied = apply_wrap(
        "python.exe", ["-m", "agent.run.skills_mcp_adapter"],
        {"SKILLS_DATA_DIR": os.path.join(os.getcwd(), "data")},
        os.path.join(os.getcwd(), "data", "sandbox", "s1"), d["wrap"])
    assert applied is True and cmd == "docker"
    seq = args
    assert "python3" in seq and seq[-2:] == ["-m", "agent.run.skills_mcp_adapter"]
    img = seq.index("python:3.12-alpine")
    assert seq.index("-e") < img < seq.index("python3"), \
        f"顺序错误（-e 应在 image 前、命令应在 image 后）: {seq}"
    # 环境变量值按挂载点映射（Windows 路径在容器内无效）
    assert "SKILLS_DATA_DIR=/repo/data" in seq


def test_apply_wrap_argv_miss_degrades():
    """容器里也没有 Windows 原生程序的位置 ⇒ 未命中翻译就原样启动（不硬套）。"""
    d = _docker_opts()
    cmd, args, _env, _cwd, applied = apply_wrap(
        r"C:/Tools/native.exe", ["--win"], {}, None, d["wrap"])
    assert applied is False and cmd.endswith("native.exe")


def test_map_path_rewrites_host_prefix_only():
    wrap = {"path_map": [[r"C:/repo", "/repo"]]}
    assert _map_path(r"C:/repo/data/skills", wrap) == "/repo/data/skills"
    assert _map_path("C:/repo/data", wrap) == "/repo/data"
    assert _map_path("/already/posix", wrap) == "/already/posix"   # 非映射前缀原样
    assert _map_path("anything", {}) == "anything"                 # 无 path_map 不炸


def test_container_unavailable_is_reported():
    be, info = select_backend("container")
    # 本机有 Docker ⇒ 真选上；没有 ⇒ 显式点名不可得必须**可见**（error + fell_back）
    if info["backend"] == "container":
        assert info["error"] is False
    else:
        assert info["error"] is True and info["fell_back"] is True
