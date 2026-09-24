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
