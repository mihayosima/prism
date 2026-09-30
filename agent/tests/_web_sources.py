"""前后端契约守卫的数据源（B4 起）。

背景
----
B4 之前，前端是 ``agent/run/chat.py`` 内联的 ``_PAGE`` 常量，因此多处
「后端设置键必须同步到前端」的契约测试直接断言 ``srv._PAGE`` 的内容。

B4 之后前端独立成 ``web/`` 工程（Vue3 + TS + Vite），``_PAGE`` 已删除。
为**保持同样的保护强度**（后端新增设置键而前端漏改时必须红灯），这些测试
改为读**前端源码文本**。这不是放宽断言，只是更换数据源。

为什么指向 ``views/settings.logic.ts``
-------------------------------------
该文件是设置页的渲染逻辑，同时承载两次结构登记 —— 与旧 ``_PAGE`` 完全对应：

- ``CONTEXT_FIELDS``       —— 字段表（标签 + 说明），旧 ``_PAGE`` 的 fields 数组；
- ``SETTINGS_NUMERIC_KEYS`` —— 保存时收集的键顺序，旧 ``_PAGE`` 的 saveSettings keys 数组。

故 ``source.count(f"'{k}'") >= 2`` 在迁移后**语义不变**（字段表一次 + 保存列表一次）。

失败模式
--------
前端源码缺失（未检出 ``web/`` 子工程）时**抛 FileNotFoundError，而非静默通过** ——
否则契约守卫会退化成永远绿灯的摆设，这正是本仓库最忌讳的"假绿"。

若日后把 ``CONTEXT_FIELDS`` / ``SETTINGS_NUMERIC_KEYS`` 迁到别的文件，本模块需同步
更新路径（会以**响亮红灯**的形式暴露，而不是静默失效）。

时间：2026-09-30 19:49 (GMT+8)
"""

from __future__ import annotations

from pathlib import Path

# 仓库根 = agent/tests/_web_sources.py → parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WEB_SRC = _REPO_ROOT / "web" / "src"

#: 承载「设置项键名」的前端源码（相对 web/src/）
_SETTINGS_SOURCE = Path("views") / "settings.logic.ts"


def settings_source_path() -> Path:
    """返回承载设置键名的前端源码路径（供报错信息与断言提示使用）。"""
    return _WEB_SRC / _SETTINGS_SOURCE


def frontend_settings_source() -> str:
    """读取前端设置页源码文本（B4 前 ``srv._PAGE`` 的等价数据源）。

    测试用法::

        from agent.tests._web_sources import frontend_settings_source

        src = frontend_settings_source()
        assert f"'{k}'" in src, f"{k} 未同步到前端 keys/字段表"
    """
    path = settings_source_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"前端源码缺失：{path}\n"
            f"「后端设置键 → 前端」契约守卫需要 web/ 子工程在场；"
            f"请确认已检出 web/ 目录，或同步更新本模块的前端源码路径。"
        )
    return path.read_text(encoding="utf-8")
