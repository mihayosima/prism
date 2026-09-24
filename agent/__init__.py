"""Agent —— slim Cordis 运行时（通用型 · everything-is-a-plugin）。

独立性：本包只 `from agent.api import …`；零外部引擎、零 Node、零三方重依赖；
离线夹具可自举（`python -m agent.run.chat --fixture` 跑通对话闭环 = 独立性硬证明）。
"""

__version__ = "0.1.0"
__agent_milestone__ = "M-A0..A6 已完成（内核 / 系统插件 / 双驱动 / 插件管理 / 工具桥 / 记忆）"

__all__ = ["__version__", "__agent_milestone__"]