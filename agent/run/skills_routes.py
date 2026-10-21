"""agent.run.skills_routes —— 技能管理面 HTTP 路由。

与运行时加载路径同一套解耦范式：路由经 ``c.root.get(Services.SKILLS)`` 反向读取
``SkillsCatalog``，不 import skills 插件；未装配 ⇒ 返回 enabled=False。


写入操作受权限档软约束；后期可升级为硬闸门。
持久化：add/remove 写 ``data/skills/<name>.md``，启停改
``data/skills_state.json``。同名冲突 / 插件来源技能移除等按决策报错。
"""

from __future__ import annotations

from agent.api import AgentError, ServiceConflictError, Services


def _valid_name(name: str) -> bool:
    """技能名合法性：非空、不含路径分隔符、不含 ``..``、不以 ``.`` 开头。

 不再要求 ``skill.`` 前缀：Agent Skills 标准用文件夹名。
 """
    if not name or name.strip() != name:
        return False
    if "/" in name or "\\" in name or ".." in name or name.startswith("."):
        return False
    return True


def register_skills_routes(app, deps) -> None:
    """把技能管理路由挂到 FastAPI ``app`` 上。"""
    c = deps.c
    JSONResponse = deps.JSONResponse

    def _cat():
        return c.root.get(Services.SKILLS)

    @app.get("/api/skills")
    async def _list_skills(q: str = ""):
        cat = _cat()
        if cat is None:
            return JSONResponse({"skills": [], "enabled": False})
        rows = cat.search(q) if q.strip() else cat.catalog()
        return JSONResponse({"skills": rows, "enabled": True})

    @app.get("/api/skills/{name}")
    async def _get_skill(name: str):
        cat = _cat()
        if cat is None:
            return JSONResponse({"error": "技能系统未启用"}, status_code=503)
        s = cat.load(name)
        if s is None:
            return JSONResponse({"error": f"未找到技能 {name!r}"}, status_code=404)
        return JSONResponse(cat._card(s))

    @app.post("/api/skills")
    async def _add_skill(body: dict):
        cat = _cat()
        if cat is None:
            return JSONResponse({"error": "技能系统未启用"}, status_code=503)
        name = str(body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "name 必填"}, status_code=400)
        # 不再强制 ``skill.`` 前缀。
        # 既有显式写了前缀的技能照原样保留，不改写。
        if not _valid_name(name):
            return JSONResponse(
                {"error": "name 非法：不得为空/含 / \\ .. 或以 . 开头"}, status_code=400)
        description = str(body.get("description") or "")
        content = str(body.get("content") or "")
        risk = str(body.get("risk") or "read")
        tags = body.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        persist = bool(body.get("persist", True))
        try:
            card = cat.add_fields(name=name, description=description,
                                  content=content, risk=risk, tags=list(tags),
                                  owner="admin", source="file", persist=persist)
        except ServiceConflictError:
            return JSONResponse({"error": f"技能 {name!r} 已存在（同名冲突）"},
                                status_code=409)
        except AgentError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "skill": card}, status_code=201)

    @app.put("/api/skills/{name}")
    async def _update_skill(name: str, body: dict):
        cat = _cat()
        if cat is None:
            return JSONResponse({"error": "技能系统未启用"}, status_code=503)
        s = cat.load(name)
        if s is None:
            return JSONResponse({"error": f"未找到技能 {name!r}"}, status_code=404)
        # 插件型技能仅允许启停，不可经管理面改内容
        if s.source == "plugin" and any(k in body for k in ("description", "content", "risk", "tags")):
            return JSONResponse({"error": "插件型技能不可经管理面编辑内容"}, status_code=400)
        if "description" in body:
            s.description = str(body["description"])
        if "content" in body:
            s.content = str(body["content"])
        if "risk" in body:
            s.risk = str(body["risk"])
        if "tags" in body:
            t = body["tags"] or []
            s.tags = list(t) if isinstance(t, list) else \
                [x.strip() for x in str(t).split(",") if x.strip()]
        persist = bool(body.get("persist", True))
        if persist and s.source == "file" and any(
                k in body for k in ("description", "content", "risk", "tags")):
            cat.persist(s)
        if "enabled" in body:
            try:
                cat.set_enabled(name, bool(body["enabled"]), persist=persist)
            except KeyError:
                return JSONResponse({"error": f"未找到技能 {name!r}"}, status_code=404)
        return JSONResponse({"ok": True, "skill": cat._card(s)})

    @app.delete("/api/skills/{name}")
    async def _remove_skill(name: str, body: dict | None = None):
        cat = _cat()
        if cat is None:
            return JSONResponse({"error": "技能系统未启用"}, status_code=503)
        persist = True
        if isinstance(body, dict):
            persist = bool(body.get("persist", True))
        try:
            cat.remove(name, persist=persist)
        except KeyError:
            return JSONResponse({"error": f"未找到技能 {name!r}"}, status_code=404)
        except AgentError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    @app.post("/api/skills/{name}/reload")
    async def _reload_skill(name: str):
        cat = _cat()
        if cat is None:
            return JSONResponse({"error": "技能系统未启用"}, status_code=503)
        s = cat.load(name)
        if s is None:
            return JSONResponse({"error": f"未找到技能 {name!r}"}, status_code=404)
        if s.source != "file":
            return JSONResponse({"error": "仅文件型技能支持 reload"}, status_code=400)
        try:
            ns = cat.reload_from_disk(name)
        except FileNotFoundError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except Exception as exc:                     # noqa: BLE001
            return JSONResponse({"error": f"重载失败：{exc}"}, status_code=400)
        return JSONResponse({"ok": True, "skill": cat._card(ns)})


__all__ = ["register_skills_routes"]
