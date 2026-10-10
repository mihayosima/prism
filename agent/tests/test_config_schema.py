"""P2 测试：配置 schema 校验（fail-fast）。

覆盖：未知顶层键、未知嵌套键、类型错（含 bool 误当 int）、合法配置通过、
可选节缺失不报错、插件自管节（_any）放行、coerce_config 补默认值。
"""

import copy

import pytest

from agent.api import ConfigError
from agent.core.config_schema import coerce_config, validate_config
from agent.profiles import BASE_AGENT_CONFIG


def _base() -> dict:
    return copy.deepcopy(BASE_AGENT_CONFIG)


def test_unknown_top_level_key_raises():
    with pytest.raises(ConfigError):
        validate_config({"foo": 1})


def test_unknown_nested_key_raises():
    # agent.budet 不在 schema（正确名为 budget）
    with pytest.raises(ConfigError):
        validate_config({"agent": {"budet": {}}})
    # agent.llm.provideer 不在 schema（正确名为 provider）
    with pytest.raises(ConfigError):
        validate_config({"agent": {"llm": {"provideer": "x"}}})


def test_type_error_raises():
    with pytest.raises(ConfigError):
        validate_config({"db_path": 123})              # 期望 str，得到 int
    with pytest.raises(ConfigError):
        validate_config({"agent": {"budget": {"max_tokens_per_run": "200"}}})  # 期望 int，得到 str
    with pytest.raises(ConfigError):
        validate_config({"agent": {"loop": {"deadline_s": "60"}}})            # 期望 int/float，得到 str


def test_bool_not_treated_as_int():
    # isinstance(True, int) 为真，但配置类型校验必须显式排除 bool
    with pytest.raises(ConfigError):
        validate_config({"agent": {"budget": {"max_tokens_per_run": True}}})


def test_valid_config_passes():
    validate_config(_base())                  # BASE_AGENT_CONFIG 本身合法
    validate_config({"db_path": "x", "agent": {}})   # 可选节缺失不报错
    validate_config({"agent": {"budget": {"max_tokens_per_run": 7}}})  # 仅给部分内核键


def test_plugin_managed_sections_allowed():
    # 插件自管节宽松放行（不校验内部键）
    validate_config({
        "windows_mcp": {"command": "x", "any_key": 1},
        "agent": {
            "react": {"max_steps": 3, "whatever": []},
            "memory": {"path": "p", "nested": {"a": 1}},
        },
    })


def test_coerce_fills_defaults_and_is_independent():
    out = coerce_config({}, defaults=BASE_AGENT_CONFIG)
    assert out["agent"]["budget"]["max_tokens_per_run"] == 200000
    assert out["agent"]["loop"]["deadline_s"] == 60
    # 独立副本：改 out 不影响 BASE_AGENT_CONFIG
    out["agent"]["budget"]["max_tokens_per_run"] = 1
    assert BASE_AGENT_CONFIG["agent"]["budget"]["max_tokens_per_run"] == 200000


def test_coerce_user_overrides_defaults():
    out = coerce_config({"agent": {"budget": {"max_tokens_per_run": 42}}},
                        defaults=BASE_AGENT_CONFIG)
    assert out["agent"]["budget"]["max_tokens_per_run"] == 42  # 用户值优先
    assert out["agent"]["loop"]["deadline_s"] == 60            # 其余仍补默认


# ---- 设置页可写键 vs schema 的一致性守卫（2026-10-10）----

def test_every_setting_key_is_registered_in_schema():
    """每个「设置页可写键」都必须在其目标节的 schema 里登记。

    ★ 这是一条**跨文件**不变式：``run/chat.py::_SETTING_META`` 登记可写键，
    经 ``_SECTION_ALIAS`` / ``plugins.<名>`` 映射到 ``agent.<节>``；而
    ``validate_config`` 对**非 _any** 节做严格键校验。任一侧漏登记 ⇒
    用户点「保存」时 ``ConfigError`` 报「未知配置键」且**整次保存失败**。

    实测踩过（2026-10-10）：``allow``/``deny`` 在 G7 那笔被登记进设置页
    （``plugin="plugins.guard"`` ⇒ ``_SECTION_ALIAS`` 映射到 ``agent.permissions``），
    但 ``_SCHEMA["agent"]["permissions"]`` 一直漏登 ⇒ 保存准入名单必失败。
    本守卫把这类漏登记挡在 CI，而不是留给用户点一次保存才发现。
    """
    from agent.core.config_schema import _SCHEMA
    from agent.run.chat import _SECTION_ALIAS, _SETTING_META

    missing: list[str] = []
    for key, meta in _SETTING_META.items():
        pid = meta["plugin"]
        base = _SECTION_ALIAS.get(pid, pid[len("plugins."):])
        node = _SCHEMA["agent"].get(base)
        if node == "_any":          # 该节不严格校验 ⇒ 无需登记
            continue
        if node is None:
            missing.append(f"{key} → agent.{base}（节不存在）")
        elif key not in node:
            missing.append(f"{key} → agent.{base}（键未登记）")
    assert not missing, (
        "设置页可写键未在 config_schema 登记（用户保存时会 fail-fast）："
        f"{missing}"
    )


def test_guard_allow_deny_setting_round_trips_validation():
    """回归：保存准入名单（permissions.allow/deny）不再被 schema 拒绝。"""
    # 真实保存路径写入的形态（_coerce_setting 的 list 分支产出 list）
    validate_config({"agent": {"permissions": {
        "allow": ["mcp.*"], "deny": [], "targets": ["*"], "default": "open",
    }}})
    # 负例：拼错键仍必须 fail-fast（守卫不能放宽成 _any）
    with pytest.raises(ConfigError):
        validate_config({"agent": {"permissions": {"alow": []}}})
