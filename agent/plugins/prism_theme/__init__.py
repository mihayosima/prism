"""插件包：prism_theme —— 极简易对话界面 + prism 暗色玻璃皮肤（可管理插件）。

理念：本包**只提供 UI 皮肤与布局**，不含任何业务逻辑。通用对话内核（chat.py）暴露一个
「主题槽」：启用本插件后，前端自动注入本包的 ``theme.css`` / ``theme.js`` / ``prism.svg``，
并把 ``<body data-theme="prism_theme">`` 挂上；卸载即恢复默认外观（core 零改动）。

- ``theme.js`` 把左侧导航收进**右侧可滑出抽屉**（默认隐藏），仅留对话主区 + 极简顶条 + 棱镜标志；
- ``theme.css`` 提供 prism 暗色玻璃拟态皮肤（光谱渐变 / 辉光 / 棱镜色散描边）。

管理：经「插件」页可启用 / 停用 / 移除（引擎式 desired/actual/delta）。
默认未启用——在插件页点「启用」（或 state 文件 enabled 加入本插件 id），皮肤即时生效。
"""

from __future__ import annotations

from agent.api import Services, plugin

# 主题资源挂在插件目录下的固定文件名上（chat.py 的「主题槽」按此约定加载）
THEME_CSS = "theme.css"
THEME_JS = "theme.js"
THEME_ICON = "prism.svg"
THEME_ID = "prism_theme"          # <body data-theme> 取值（= 插件包目录名）


@plugin
class PrismTheme:
    """prism 暗色玻璃皮肤 + 极简对话布局（纯 UI，无业务依赖）。"""

    name = "plugins.prism_theme"
    version = "0.1.0"
    layer = "business"
    provides = [Services.UI_THEME]
    description = "prism 暗色玻璃皮肤与极简对话布局（可滑出右侧抽屉收纳其余功能）"

    async def apply(self, ctx) -> None:
        # 仅登记「主题提供方」；真正的皮肤/布局由前端主题槽注入本包资源完成。
        ctx.provide(Services.UI_THEME, self)
        ctx.state.setdefault("ui_theme_id", THEME_ID)

    async def start(self, ctx):
        pass


__all__ = ["PrismTheme", "THEME_ID", "THEME_CSS", "THEME_JS", "THEME_ICON"]
