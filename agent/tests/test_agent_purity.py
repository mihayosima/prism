"""纯度守卫 —— 内核树零业务词；agent.api 是唯一出口。

内核 = 纯机制：业务词只允许出现在 agent/plugins/ 的业务插件、fixtures/ 与领域文档，
不得进入 agent/core 与 agent/api；内核也不得 import 外部引擎 / 大模型 SDK。
"""
from __future__ import annotations

from pathlib import Path

AGENT_PKG = Path(__file__).resolve().parent.parent          # agent/
CORE = AGENT_PKG / "core"

# 业务词清单（来自业务文档词表）：出现在内核树即判污染
BUSINESS_TERMS = [
    "选品", "策略", "候选", "评分", "排榜", "短名单", "技能库", "爆款",
    "互补", "替代", "升级", "消耗", "季节", "场景", "潮流", "科技", "健康",
    "美学", "社交", "情绪", "痛点", "家居", "儿童", "效率", "户外",
    "substitution", "complementary", "upgrade", "consumable",
    "bestseller", "sku", "tiktok", "product_id", "picks", "skill",
]


def _core_py_files() -> list[Path]:
    out = sorted(CORE.rglob("*.py"))
    out.append(AGENT_PKG / "api.py")
    return [fp for fp in out if "__pycache__" not in fp.parts]


def test_core_has_no_business_terms():
    bad: list[tuple[str, str]] = []
    for fp in _core_py_files():
        text = fp.read_text(encoding="utf-8").lower()
        for term in BUSINESS_TERMS:
            if term.lower() in text:
                bad.append((fp.name, term))
    assert not bad, f"内核出现业务词: {bad}"


def test_core_never_imports_engine():
    bad: list[tuple[str, str]] = []
    for fp in _core_py_files():
        text = fp.read_text(encoding="utf-8")
        for pat in ("from engine", "import engine"):
            if pat in text:
                bad.append((fp.name, pat))
    assert not bad, f"内核 import 了引擎: {bad}"


def test_core_never_imports_sys_or_plugins():
    """K0 分层守卫：内核（core + 唯一出口 api）不得反向依赖 sys / plugins。

    解耦的硬约束——内核是纯机制，不得获知业务/系统插件的具体实现位置。
    """
    bad: list[tuple[str, str]] = []
    for fp in _core_py_files():
        text = fp.read_text(encoding="utf-8")
        for pat in ("from agent.sys", "from agent.plugins",
                    "import agent.sys", "import agent.plugins"):
            if pat in text:
                bad.append((fp.name, pat))
    assert not bad, f"内核反向依赖 sys/plugins: {bad}"


# 领域身份词：**不是**污染（多租户/会话身份是通用领域模型，不是电商业务）。
# 列在这里是为了让守卫的"白名单"显式化 —— 新增领域词时必须在此登记，
# 避免"守卫没报"被误当成"这就合规"。
DOMAIN_IDENTITY_TERMS = {"tenant", "user", "session", "account", "member"}


def test_core_business_terms_are_domain_neutral():
    """★ 正交断言：内核不得出现**领域业务节名**（而不仅是词表里的电商词）。

    为什么需要这条：``test_core_has_no_business_terms`` 只做**子串匹配**，
    于是有两个方向的漏洞：
      ① **假阳性**：注释里写"策略"二字就转红（本项目已因此踩坑两次，
         改的是注释措辞而非删断言）；
      ② **假阴性**：真正的领域业务节名（如 ``shop``/``order``/``product``）
         不在电商词表里 ⇒ 守卫绿灯而内核已沾业务。
    本条只拦"**代码里的标识符字面量**"，不扫注释与 docstring ⇒ 消掉 ①。
    """
    import ast
    import re

    # 领域业务标识符（电商/交易域）。多租户身份词不在此列（见 DOMAIN_IDENTITY_TERMS）。
    # ⚠️ `order` 已被移出：``_Listener.order`` 是"执行序"（排序语义），不是"订单"。
    #    同形异义是这类词表的固有陷阱 —— 加入前必须确认内核里的用法无歧义。
    _DOMAIN_WORDS = ("shop", "cart", "checkout", "sku", "price", "goods",
                     "customer", "mall", "coupon", "refund", "inventory", "listing")
    # ★ 用**词边界**而非子串：`"shop_id"` / `product_id` / `order_item` 都是污染，
    #   但裸子串 `"shop"` 匹配不到它们（我第一版就因此让破坏实验漏网）。
    domain_literals = re.compile(
        r'"[A-Za-z_]*(' + "|".join(_DOMAIN_WORDS) + r')[A-Za-z_]*"', re.I)

    bad: list[tuple[str, int, str]] = []
    for fp in _core_py_files():
        src = fp.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:                        # pragma: no cover
            continue
        # 只看字符串字面量与属性名（跳过注释与 docstring）
        for node in ast.walk(tree):
            text = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            elif isinstance(node, ast.Attribute):
                text = node.attr
            if not text:
                continue
            m = domain_literals.fullmatch(f'"{text}"')
            if m:
                bad.append((fp.name, getattr(node, "lineno", 0), m.group(1)))

    assert not bad, (
        f"内核出现领域业务字面量（守卫词表只覆盖电商词，这些是漏网）: {bad}\n"
        f"若确属通用领域概念（如多租户身份），请登记到 DOMAIN_IDENTITY_TERMS 并说明理由。")
    # 自检：白名单里的词不应被误判（防止把这条守卫写死）
    for identity in DOMAIN_IDENTITY_TERMS:
        assert not domain_literals.fullmatch(f'"{identity}"'), (
            f"{identity} 被误列为领域业务词 —— 它属于通用领域身份（见 DOMAIN_IDENTITY_TERMS）")


def test_core_files_only_in_core_and_api():
    """内核实现只应存在于 agent/core 与 agent/api（出口）；sys/plugins 等不得 import core 内部。"""
    allowed = {fp.name for fp in _core_py_files()}
    assert "context.py" in allowed and "container.py" in allowed and "events.py" in allowed
    assert "errors.py" in allowed and "api.py" in allowed