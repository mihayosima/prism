"""Skill 插件单元测试（阶段 6）。

聚焦可独立验证的真实行为：注册/冲突、查询/加载、代码型 invoke、handle_meta（load 注入
skill_docs / invoke 调 run / plan 模式拒写）、索引预算、持久化增删、loader 往返、react
_tools_for_step 合并技能常驻工具、TaskContext.skill_docs 字段存在。

不依赖真实 LLM / 真实容器装配。
"""

from __future__ import annotations

import pytest

from agent.api import ServiceConflictError, Services, StateKeys
from agent.plugins.react.loop import ReactLoop, TaskContext
from agent.plugins.skills.catalog import SkillsCatalog
from agent.plugins.skills.loader import (_state_path, delete_skill_file,
                                         discover_skills, load_state,
                                         parse_skill_file, save_state,
                                         write_skill_file)
from agent.plugins.skills.model import Skill, skill


# ---- 最小假 ctx（仅用于 _tools_for_step 的 get(Services.SKILLS) 探测） ----
class _Ctx:
    def __init__(self, skills=None):
        self._skills = skills
        self.state: dict = {}

    def get(self, key, default=None):
        if key == Services.SKILLS:
            return self._skills
        return default


def _make_catalog(tmp_path):
    cat = SkillsCatalog(tmp_path)
    cat.register(skill("skill.alpha", "演示 alpha", content="步骤1：做A\n步骤2：做B", risk="read"))
    return cat


def test_catalog_register_conflict():
    cat = SkillsCatalog(__import__("pathlib").Path("/tmp/__noop__"))
    cat.register(skill("skill.dup", "x"))
    with pytest.raises(ServiceConflictError):
        cat.register(skill("skill.dup", "y"))


def test_catalog_search_load():
    cat = _make_catalog(_tmp())
    assert cat.load("skill.alpha") is not None
    assert cat.load("nope") is None
    hits = cat.search("alpha")
    assert any(h["name"] == "skill.alpha" for h in hits)
    # 空关键词返回全部
    assert len(cat.search("")) == 1


def test_catalog_invoke_runs_code():
    cat = _make_catalog(_tmp())
    captured = {}

    def _run(ctx, x=0):
        captured["x"] = x
        return {"echo": x * 2}

    cat.register(skill("skill.calc", "计算", risk="read", run=_run, source="plugin"))
    res = cat.invoke("skill.calc", None, x=21)
    assert res["ok"] is True
    assert res["result"] == {"echo": 42}
    assert captured["x"] == 21
    # 无代码入口的技能 invoke 失败
    assert cat.invoke("skill.alpha", None)["ok"] is False


def test_handle_meta_load_appends_skill_docs():
    cat = _make_catalog(_tmp())
    task = TaskContext()
    assert task.skill_docs == []
    text, err = cat.handle_meta(task, _call("load", "skill.alpha"), _Ctx(cat))
    assert err is False
    assert len(task.skill_docs) == 1
    assert "步骤1" in task.skill_docs[0]


def test_handle_meta_invoke():
    cat = _make_catalog(_tmp())

    def _run(ctx, a=1):
        return {"a": a}

    cat.register(skill("skill.act", "动作", risk="write", run=_run, source="plugin"))
    text, err = cat.handle_meta(task := TaskContext(), _call("invoke", "skill.act", {"a": 5}), _Ctx(cat))
    assert err is False
    assert "a" in text and "5" in text


def test_handle_meta_plan_mode_rejects_write():
    cat = _make_catalog(_tmp())

    def _run(ctx, a=1):
        return {"a": a}

    cat.register(skill("skill.act", "动作", risk="write", run=_run, source="plugin"))
    ctx = _Ctx(cat)
    ctx.state[StateKeys.PERMISSION_MODE] = "plan"
    text, err = cat.handle_meta(TaskContext(), _call("invoke", "skill.act"), ctx)
    assert err is True
    assert "plan" in text
    # 同一技能若 risk=read 则 plan 下仍放行
    cat.register(skill("skill.readonly", "只读", risk="read", run=_run, source="plugin"))
    t2, e2 = cat.handle_meta(TaskContext(), _call("invoke", "skill.readonly"), ctx)
    assert e2 is False


def test_handle_meta_unknown_action_and_missing():
    cat = _make_catalog(_tmp())
    t1, e1 = cat.handle_meta(TaskContext(), _call("frobnicate", "skill.alpha"), _Ctx(cat))
    assert e1 is True
    t2, e2 = cat.handle_meta(TaskContext(), _call("load", "nope"), _Ctx(cat))
    assert e2 is True


def test_index_block_budget():
    cat = _make_catalog(_tmp())
    for i in range(5):
        cat.register(skill(f"skill.item{i}", f"长描述用于测试索引块膨胀 {'x' * 50}", risk="read"))
    block = cat.index_block(max_chars=120)
    # 超出预算应被截断并附提示
    assert len(block) <= 200
    assert "截断" in block or "可用技能" in block
    # 空目录返回空串
    assert SkillsCatalog(_tmp()).index_block() == ""


def test_meta_tools_shape():
    cat = _make_catalog(_tmp())
    mt = cat.meta_tools()
    assert len(mt) == 1
    assert mt[0]["name"] == "skill"
    assert "action" in mt[0]["params"] and "name" in mt[0]["params"]


def test_persist_add_remove(tmp_path):
    cat = _make_catalog(tmp_path)
    s = skill("skill.persist", "持久化", content="c", risk="read")
    cat.add(s, persist=True)
    # 文件已写出
    assert (tmp_path / "skills" / "skill.persist.md").exists()
    # 移除（写盘）
    cat.remove("skill.persist", persist=True)
    assert not (tmp_path / "skills" / "skill.persist.md").exists()
    # 插件来源不可经管理面移除
    cat.register(skill("skill.plugin", "p", source="plugin"))
    with pytest.raises(Exception):
        cat.remove("skill.plugin", persist=False)
    # 启停持久化
    cat.set_enabled("skill.alpha", False, persist=True)
    st = load_state(tmp_path)
    assert "skill.alpha" in st
    assert cat.load("skill.alpha").enabled is False
    cat.set_enabled("skill.alpha", True, persist=True)
    assert "skill.alpha" not in load_state(tmp_path)


def test_loader_roundtrip(tmp_path):
    p = write_skill_file(tmp_path, skill("skill.file", "文件技能", content="正文", risk="read", tags=["a", "b"]))
    skills = discover_skills(tmp_path)
    assert len(skills) == 1
    s = skills[0]
    assert s.name == "skill.file"
    assert s.source == "file"
    assert s.content == "正文"
    assert s.tags == ["a", "b"]
    # 删除
    assert delete_skill_file(tmp_path, "skill.file") is True
    assert discover_skills(tmp_path) == []


def test_loader_failed_parse_skipped(tmp_path):
    bad = tmp_path / "skills"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "broken.md").write_text("不是合法的frontmatter但无---起始\n正文", encoding="utf-8")
    good = write_skill_file(tmp_path, skill("skill.ok", "ok", content="ok"))
    skills = discover_skills(tmp_path)
    names = {s.name for s in skills}
    assert "skill.ok" in names
    assert len(skills) == 1  # 损坏文件被跳过，不拖垮


# -------------------- Agent Skills 标准对齐（格式） -------------------- #

def _write_dir_skill(root, dirname: str, fm: str, body: str = "正文") -> Path:
    """写一个目录式技能 ``skills/<dirname>/SKILL.md``（Agent Skills 标准布局）。"""
    d = root / "skills" / dirname
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(fm + body, encoding="utf-8")
    return p


def test_loader_directory_layout(tmp_path):
    """目录式：``skills/<name>/SKILL.md``，名称取目录名，basedir 指向该目录。"""
    _write_dir_skill(tmp_path, "docker-build", "---\ndescription: 构建镜像\nrisk: read\n---\n\n")
    skills = discover_skills(tmp_path)
    assert [s.name for s in skills] == ["docker-build"]
    assert skills[0].basedir == str(tmp_path / "skills" / "docker-build")
    assert skills[0].description == "构建镜像"


def test_loader_no_forced_skill_prefix(tmp_path):
    """不再强制加 ``skill.`` 前缀；显式写了前缀的既有技能照原样保留。"""
    _write_dir_skill(tmp_path, "web-search", "---\nname: web-search\ndescription: d\n---\n\n")
    write_skill_file(tmp_path, skill("skill.legacy", "旧式", content="c"))
    assert {s.name for s in discover_skills(tmp_path)} == {"web-search", "skill.legacy"}


def test_loader_multiline_description(tmp_path):
    """``description: |`` 多行块须完整保留——它是模型做 Match 的唯一依据。"""
    fm = ("---\nname: ml\ndescription: |\n"
          "  构建与推送 Docker 镜像。\n"
          "  Use when: 需要打包镜像、优化 Dockerfile。\n"
          "  Triggers include: docker build, 镜像瘦身\n"
          "risk: read\n---\n\n")
    p = _write_dir_skill(tmp_path, "ml", fm)
    s = parse_skill_file(p)
    assert "Use when" in s.description and "Triggers include" in s.description
    assert s.description.count("\n") == 2, f"多行被折叠了: {s.description!r}"
    assert s.risk == "read", "块标量之后应能继续解析普通键"


def test_loader_dir_and_flat_conflict_skipped(tmp_path):
    """两种布局同名 ⇒ 告警跳过（目录式优先），不让容器装配崩。"""
    _write_dir_skill(tmp_path, "dup", "---\ndescription: 目录式\n---\n\n")
    write_skill_file(tmp_path, skill("dup", "扁平式", content="c"))
    skills = discover_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].description == "目录式"


def test_write_back_to_directory_does_not_split(tmp_path):
    """编辑目录式技能写回原 SKILL.md，不另存扁平副本（否则下次扫描重名）。"""
    _write_dir_skill(tmp_path, "edit-me", "---\ndescription: d\n---\n\n旧正文")
    s = discover_skills(tmp_path)[0]
    s.content = "新正文"
    write_skill_file(tmp_path, s)
    assert not (tmp_path / "skills" / "edit-me.md").exists(), "不应产生扁平副本"
    assert "新正文" in (tmp_path / "skills" / "edit-me" / "SKILL.md").read_text(encoding="utf-8")
    assert len(discover_skills(tmp_path)) == 1


def test_multiline_description_roundtrip(tmp_path):
    """多行 description 写回后仍可被完整解析（往返不丢行）。"""
    cat = _make_catalog(tmp_path)
    cat.add(skill("ml-rt", "第一行\nUse when: 第二行", content="c", risk="read"),
            persist=True)
    again = discover_skills(tmp_path)
    assert len(again) == 1
    assert again[0].description == "第一行\nUse when: 第二行"


def test_react_tools_for_step_merges_skill_tool():
    cat = _make_catalog(_tmp())
    ctx = _Ctx(cat)
    task = TaskContext()
    # 非懒加载路径：直接检查 _tools_for_step（懒加载内）合并了 skill 工具
    out = ReactLoop._tools_for_step([{"name": "real_tool", "description": "d"}], task, None, ctx)
    names = [t["name"] for t in out]
    assert "skill" in names
    assert "real_tool" in names
    # 无 SKILLS 时不混入
    out2 = ReactLoop._tools_for_step([{"name": "real_tool", "description": "d"}], task, None, _Ctx(None))
    assert "skill" not in [t["name"] for t in out2]


def test_taskcontext_has_skill_docs():
    t = TaskContext()
    assert hasattr(t, "skill_docs")
    assert t.skill_docs == []


def _tmp():
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    return d


def _call(action: str, name: str, params: dict | None = None):
    """构造一个最小 tool_call 替身（仅含 handle_meta 所需的属性）。"""

    class _C:
        name = "skill"
        id = "call_1"

        def __init__(self):
            # 在实例方法中构造 arguments：此处 name 解析为闭包参数，而非上面的类属性
            # name="skill"（否则类体内 name 会被类属性遮蔽，导致 arguments["name"]=="skill"）。
            self.arguments = {"action": action, "name": name}
            if params is not None:
                self.arguments["params"] = params

    return _C()
