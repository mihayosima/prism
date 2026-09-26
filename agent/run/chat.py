"""agent.run.chat —— Agent 通用对话外设（web + 无头）。

前端参考 agent 对话形态（step/tool 轨迹、事件/权限/记忆/工具视图），**不特化为任何领域**：
通用能力为 ping / tool_call / chat；领域能力经「已启用的工具插件」（如 windows-mcp）挂入工具目录。

用法：
    python -m agent.run.chat                    # 网页（默认 127.0.0.1:8720，deepseek）
    python -m agent.run.chat --fixture           # 离线夹具（不动真 API）
    python -m agent.run.chat --once "你好"        # 无头跑一次
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from agent.api import Services, StateKeys
from agent.profiles import build_agent
from agent.sys.progress import tap_progress


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", default="deepseek", help="llm provider（deepseek | fixture）")
    p.add_argument("--model", default="deepseek-flash",
                   help="deepseek 模型名（线上支持 deepseek-flash / deepseek-v4-pro）")
    p.add_argument("--fixture", action="store_true", help="强制离线夹具（不触真 API）")
    p.add_argument("--db", default="data/agent_chat.db")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8720)
    p.add_argument("--once", default=None, help="无头模式：跑一条消息后退出并打印")
    return p.parse_args(argv)


def _cfg(args: argparse.Namespace) -> dict:
    provider = "fixture" if args.fixture else args.provider
    if provider == "fixture":
        return {"db_path": args.db, "agent": {"llm": {"provider": "fixture"}}}
    return {"db_path": args.db,
            "agent": {"llm": {"provider": provider, "model": args.model}}}


# ---- 插件配置覆盖（前端「设置」持久化；与插件启停期望态分离，单文件存储） ----
_OVERRIDE_FILE = Path("data/agent_plugin_config.json")


def _load_overrides() -> dict:
    """读取用户对插件运行时配置的覆盖（data/agent_plugin_config.json）。

    形态：``{ "plugins.<名>": {键: 值, ...}, ... }``；缺失/损坏返回空 dict（不裸崩）。
    """
    if not _OVERRIDE_FILE.exists():
        return {}
    try:
        return json.loads(_OVERRIDE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 —— 损坏按空处理，不阻断启动
        return {}


def _save_overrides(data: dict) -> None:
    _OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _OVERRIDE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_OVERRIDE_FILE)


def _merge_cfg(args: argparse.Namespace) -> dict:
    """基础 config（_cfg）+ 用户覆盖，注入 build_agent。

    覆盖按 ``plugins.<名>`` → ``agent.<名>`` 映射并入对应插件配置节（均为 _any，免改 schema）。
    """
    cfg = _cfg(args)
    ov = _load_overrides()
    agent = cfg.setdefault("agent", {})
    for pid, pconf in (ov or {}).items():
        if not isinstance(pid, str) or not pid.startswith("plugins."):
            continue
        if not isinstance(pconf, dict):
            continue
        base = pid[len("plugins."):]
        node = agent.setdefault(base, {})
        node.update(pconf)
    return cfg


# ---- 上下文管理设置项 schema（前端「设置 → 上下文管理」可调） ----
# 校验/钳制：键 → (类型, 下限, 上限)；归属插件由 _PLUGIN_OF_KEY 路由。
_SETTING_SCHEMA = {
    "compact_trigger_tokens": (int, 256, 1_000_000),      # 来自 plugins.short_term
    "max_context_tokens": (int, 1024, 1_000_000),
    "reserve_output_tokens": (int, 0, 1_000_000),
    "memory_ratio": (float, 0.01, 0.99),
    "history_ratio": (float, 0.01, 0.99),
    "source_clip_tokens": (int, 0, 1_000_000),
    # ---- S3：循环内（react）上下文治理 ----
    "clip_max_tokens": (int, 0, 1_000_000),            # 单条观察超此值即首尾折叠
    "clip_head_tokens": (int, 0, 1_000_000),           # 折叠后头部保留
    "clip_tail_tokens": (int, 0, 1_000_000),           # 折叠后尾部保留
    "react_keep_recent": (int, 0, 1000),                  # 最近 N 条链原文保留
    "react_min_chain_tokens": (int, 0, 1_000_000),        # 循环内 chain 保底额度
}
_PLUGIN_OF_KEY = {
    "compact_trigger_tokens": "plugins.short_term",
    "max_context_tokens": "plugins.context",
    "reserve_output_tokens": "plugins.context",
    "memory_ratio": "plugins.context",
    "history_ratio": "plugins.context",
    "source_clip_tokens": "plugins.context",
    "clip_max_tokens": "plugins.context",
    "clip_head_tokens": "plugins.context",
    "clip_tail_tokens": "plugins.context",
    "react_keep_recent": "plugins.context",
    "react_min_chain_tokens": "plugins.context",
}
_SETTING_DEFAULTS = {
    "compact_trigger_tokens": 12000,
    "max_context_tokens": 24000,
    "reserve_output_tokens": 4000,
    "memory_ratio": 0.10,
    "history_ratio": 0.60,
    "source_clip_tokens": 8192,
    "clip_max_tokens": 1000,
    "clip_head_tokens": 500,
    "clip_tail_tokens": 500,
    "react_keep_recent": 6,
    "react_min_chain_tokens": 2048,
}


def _coerce_setting(key: str, raw) -> int | float:
    if key not in _SETTING_SCHEMA:
        raise ValueError(f"未知设置项: {key}")
    typ, lo, hi = _SETTING_SCHEMA[key]
    val = typ(raw)
    if val < lo or val > hi:
        raise ValueError(f"{key} 须落在 [{lo}, {hi}]")
    return val


async def _boot(args: argparse.Namespace):
    from agent.sys.plugin_manager import PluginManager

    pm = PluginManager(plugin_dir="agent/plugins", state_file="data/agent_plugin_state.json")
    c = build_agent(config=_merge_cfg(args), plugin_mgr=pm)
    await c.assemble()
    await c.start()
    return c, pm


# ---------------------------------------------------------------------------
# 无头模式
# ---------------------------------------------------------------------------

async def _once(args: argparse.Namespace) -> int:
    c, _pm = await _boot(args)
    try:
        reply = await c.root.chat.send(c.root, args.once)
        print("\n" + ("=" * 30) + "\n" + reply.text)
        print(f"\n[步骤] intent={reply.plan.get('intent')} ok={reply.ok}"
              f" tokens={reply.tokens_in}in/{reply.tokens_out}out")
        if reply.error:
            print(f"[error] {reply.error}")
        print("=" * 30)
        return 0 if reply.ok else 1
    finally:
        await c.teardown()


# ---------------------------------------------------------------------------
# Web 形态（dsh 风格通用对话面）
# ---------------------------------------------------------------------------

_PAGE = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>prism</title>
<style>
:root{
  --brand:#4176e6; --brand2:#5686fe; --brand-soft:#edf3ff; --brand-ink:#2b53b8;
  --bg:#f5f6f8; --card:#fff; --line:#e2e6ee; --line2:#eef1f6;
  --ink:#111827; --muted:#6b7280; --muted2:#9aa1ad;
  --dark:#151517; --dark2:#202227; --dark-ink:#e2e8f0;
  --ok:#22c55e; --ok-soft:#e9f9ef; --warn:#f7ad31; --warn-soft:#fff4e0; --err:#ef4444; --err-soft:#fdecec;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --r:10px; --r2:6px;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{font:14px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink)}
.top{display:flex;align-items:center;gap:14px;padding:0 20px;height:56px;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:15px}
.brand .mark{width:26px;height:26px;border-radius:7px;background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;display:grid;place-items:center;font-size:13px;font-weight:800}
.mono{font-family:var(--mono);font-size:11.5px;color:var(--muted)}
.top .spacer{flex:1}
.pill{background:var(--brand-soft);color:var(--brand-ink);border:1px solid #d7e5ff;border-radius:999px;padding:4px 12px;font-size:12px;font-family:var(--mono)}
.wrap{display:flex;height:calc(100vh - 56px)}
.nav{width:190px;border-right:1px solid var(--line);background:var(--card);padding:12px 10px;display:flex;flex-direction:column;gap:2px}
.nav h6{font-size:10.5px;color:var(--muted2);font-weight:600;letter-spacing:.04em;margin:10px 8px 4px}
.nav-item{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:8px;color:var(--ink);cursor:pointer;font-size:13px;border:0;background:none;text-align:left;width:100%}
.nav-item:hover{background:var(--bg)}
.nav-item.active{background:var(--brand);color:#fff}
.nav-item .ic{width:16px;height:16px;opacity:.85}
.nav-foot{margin-top:auto;padding:12px 10px;font-size:11px;color:var(--muted2);white-space:pre-wrap;font-family:var(--mono)}
.main{flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden}
section[data-view]{display:none;flex:1;overflow:auto;min-height:0}
section[data-view].active{display:flex;flex-direction:column}
/* ------- chat ------- */
.chat{max-width:920px;width:100%;margin:0 auto;flex:1;display:flex;flex-direction:column;padding:18px 20px 0}
.messages{flex:1;overflow:auto;display:flex;flex-direction:column;gap:16px;padding:4px 2px 18px}
.msg{max-width:82%}
.msg.user{align-self:flex-end}
.msg.user .bubble{background:var(--brand);color:#fff;border-radius:14px 14px 4px 14px;padding:10px 14px}
.msg.agent{align-self:flex-start;width:100%}
.msg .who{font-size:11px;color:var(--muted);margin-bottom:5px}
.msg.user .who{text-align:right}
.text{background:var(--card);border:1px solid var(--line);border-radius:4px 14px 14px 14px;padding:12px 14px;white-space:normal;line-height:1.65;font-size:13.5px}
.text pre{display:inline-block;background:var(--dark);color:var(--dark-ink);border-radius:6px;padding:2px 7px;font-family:var(--mono);font-size:12px}
.text code{background:var(--brand-soft);color:var(--brand-ink);border-radius:4px;padding:1px 5px;font-family:var(--mono);font-size:12px}
.text b{font-weight:600}
.msg.user .text{display:none}
/* step trace（tool calls，dsh 签名） */
.steps{margin-top:8px;border:1px solid var(--line);border-radius:var(--r);background:var(--card);overflow:hidden}
.step{border-bottom:1px solid var(--line);font-size:12.5px}
.step:last-child{border-bottom:0}
.step .top{display:flex;align-items:center;gap:8px;padding:7px 12px;height:auto;background:var(--card);border:0}
.step .name{font-family:var(--mono);color:var(--ink)}
.step .dot{width:7px;height:7px;border-radius:50%}
.step .dot.ok{background:var(--ok)} .step .dot.err{background:var(--err)} .step .dot.run{background:var(--warn)}
.step details{margin:0 12px 8px}
.step summary{font-size:11.5px;color:var(--muted);cursor:pointer}
.step pre{margin:6px 0 0;background:#0d0f13;color:#c9d6ec;padding:10px 12px;border-radius:8px;overflow:auto;font-family:var(--mono);font-size:11.5px;white-space:pre-wrap}
.chips{display:flex;flex-wrap:wrap;gap:8px;padding:0 2px 10px}
.chip{background:var(--card);border:1px solid var(--line);color:var(--ink);border-radius:999px;padding:6px 13px;font-size:12.5px;cursor:pointer;transition:.12s}
.chip:hover{border-color:var(--brand2);color:var(--brand-ink);background:var(--brand-soft)}
.inputrow{padding:0 2px 18px;display:flex;gap:10px;align-items:flex-end}
textarea{flex:1;font:inherit;border:1px solid var(--line);border-radius:12px;padding:11px 13px;resize:none;min-height:48px;max-height:160px;outline:none}
textarea:focus{border-color:var(--brand2)}
.btn{background:var(--brand);color:#fff;border:0;padding:11px 20px;border-radius:12px;cursor:pointer;font-size:13.5px;font-weight:600}
.btn:hover{background:var(--brand-ink)}
.btn:disabled{opacity:.55;cursor:not-allowed}
.hint{font-size:11px;color:var(--muted2);margin:-14px 4px 14px}
/* ------- generic views ------- */
.panel{max-width:960px;width:100%;margin:0 auto;padding:18px 20px}
h3{font-size:13.5px;margin:0 0 12px;color:var(--ink)}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:var(--r);overflow:hidden;border:1px solid var(--line)}
th,td{padding:8px 11px;border-bottom:1px solid var(--line2);text-align:left;font-size:12.5px;vertical-align:top}
th{background:#f8fafc;color:var(--muted);font-weight:600}
tr:last-child td{border-bottom:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:13px 15px;margin-bottom:8px}
.card b{font-weight:600}
.tag{font-size:11px;border-radius:6px;padding:2px 8px;font-family:var(--mono)}
.tag.ok{background:var(--ok-soft);color:#188a46} .tag.err{background:var(--err-soft);color:var(--err)}
.tag.run{background:var(--warn-soft);color:#a16207} .tag.blue{background:var(--brand-soft);color:var(--brand-ink)}
.tag.off{background:var(--bg);color:var(--muted2);border:1px solid var(--line)}
.muted{color:var(--muted)} .e{color:var(--err)}
.empty{color:var(--muted2);font-size:12.5px}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.filters .chip.active{background:var(--brand);color:#fff;border-color:var(--brand)}
pre.json{background:#0d0f13;color:#c9d6ec;padding:10px 12px;border-radius:8px;overflow:auto;font-family:var(--mono);font-size:11.5px}
</style></head><body>
<div class="top">
  <div class="brand"><div class="mark">◈</div>prism</div>
  <span class="mono" id="session"></span>
  <div class="spacer"></div>
  <span class="pill" id="model">…</span>
  <button class="chip" id="newSession" style="margin-left:10px">新会话</button>
</div>
<div class="wrap">
<nav class="nav">
  <h6>工作台</h6>
  <button class="nav-item active" data-view="chat"><span class="ic">💬</span>会话</button>
  <h6>机器</h6>
  <button class="nav-item" data-view="events"><span class="ic">📡</span>事件</button>
  <button class="nav-item" data-view="perms"><span class="ic">🔐</span>权限</button>
  <button class="nav-item" data-view="plugins"><span class="ic">🔌</span>插件</button>
  <button class="nav-item" data-view="memory"><span class="ic">🧠</span>记忆</button>
  <button class="nav-item" data-view="tools"><span class="ic">🛠️</span>工具</button>
  <button class="nav-item" data-view="settings"><span class="ic">⚙️</span>设置</button>
  <div class="nav-foot" id="machine">…</div>
</nav>
<div class="main">
  <section data-view="chat" class="active">
    <div class="chat">
      <div class="messages" id="messages"></div>
      <div class="chips" id="chips"></div>
      <div class="hint">Enter 发送 · Shift+Enter 换行</div>
      <div class="inputrow">
        <textarea id="txt" rows="2" placeholder="告诉 Agent 你要做什么……或点下面的快捷能力"></textarea>
        <button class="btn" id="send">发送</button>
      </div>
    </div>
  </section>
  <section data-view="events">
    <div class="panel">
      <div class="filters" id="evFilters">
        <button class="chip active" data-ev="all">全部</button>
        <button class="chip" data-ev="agent/permission/decision">权限</button>
        <button class="chip" data-ev="agent/chat">会话</button>
        <button class="chip" data-ev="agent/turn">运行</button>
      </div>
      <div id="eventsBox"></div>
    </div>
  </section>
  <section data-view="perms"><div class="panel" id="permsBox"></div></section>
  <section data-view="memory"><div class="panel" id="memoryBox"></div></section>
  <section data-view="tools"><div class="panel" id="toolsBox"></div></section>
  <section data-view="settings"><div class="panel" id="settingsBox"></div></section>
  <section data-view="plugins">
    <div class="panel">
      <div class="rowline" style="display:flex;gap:10px;align-items:center;margin-bottom:12px">
        <input id="pname" placeholder="插件名（如 hello_probe）" style="flex:1;min-width:160px">
        <input id="pprovides" placeholder="provides（逗号分隔，留空默认 svc）" style="flex:1">
        <button class="btn ghost" id="pscaffold">新增插件</button>
      </div>
      <div id="pluginsBox"></div>
    </div>
  </section>
</div>
</div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const json2=(o)=>JSON.stringify(o,null,2);
function md(t){let h=esc(t).replace(/```([\s\S]*?)```/g,(_,c)=>'<br><pre>'+c+'</pre><br>');
  h=h.replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>');
  h=h.replace(/^#{1,3} (.*)$/gm,'<b>$1</b>');h=h.replace(/^- (.*)$/gm,'<span style="color:var(--muted)">•</span>&nbsp;$1');
  return '<div class="text">'+h+'</div>';}
function stepRow(title,kind,status,payload){
  const ok=status==='ok', dot=ok?'ok':(status==='run'?'run':'err');
  return '<div class="step"><div class="top"><span class="dot '+dot+'"></span><span class="name">'+esc(title)+'</span>'
    +'<span class="tag '+(ok?'ok':'err')+'">'+esc(status)+'</span></div>'
    +'<details><summary>查看 '+esc(kind)+' 载荷</summary><pre>'+esc(payload)+'</pre></details></div>';}
function addMsg(who, text, evid){
  const d=document.createElement('div');d.className='msg '+who;
  if(who==='user'){d.innerHTML='<div class="who">你</div><div class="bubble">'+esc(text)+'</div>';}
  else{
    let steps='';
    if(evid){
      const plan=evid.plan||{}, exec=evid.evidence||{};
      steps=stepRow('plan','plan.json',exec.error?'err':'ok',
        json2({intent:plan.intent, tool:plan.tool||null, args:plan.args||null}));
      if(exec.intent){
        const name = exec.intent==='tool_call' ? ('tool:'+(exec.tool||plan.tool||'?')) : 'ctx.'+exec.intent;
        const brief = exec.ok ? (exec.result||exec.ping||exec.note||'ok') : String(exec.error||'denied');
        steps+=stepRow('execute: '+name,'evidence.json',exec.ok?'ok':'err', json2({ok:exec.ok, brief}));
      }
      steps+=stepRow('narrate','evidence.evidence', evid.tokens_out>0?'ok':'err',
        json2({tokens_in:evid.tokens_in, tokens_out:evid.tokens_out}));
    }
    d.innerHTML='<div class="who">Agent</div>'+md(text)+(steps?'<div class="steps">'+steps+'</div>':'');
  }
  $('#messages').appendChild(d);
  const m=$('.messages');m.scrollTop=m.scrollHeight;}
const chips=['ping 健康检查','有哪些工具','你能做什么'];
let _ctrl=null,_streaming=false;
function addStreamMsg(){
  const d=document.createElement('div');d.className='msg agent';
  d.innerHTML='<div class="who">Agent</div>'
    +'<details class="think" open><summary>💭 思考过程</summary><div class="tk" style="white-space:pre-wrap;font-size:12px;color:#8a8a8a;"></div></details>'
    +'<div class="bubble sb">…</div><div class="steps ss"></div>';
  $('#messages').appendChild(d);
  const m=$('.messages');m.scrollTop=m.scrollHeight;
  const bub=d.querySelector('.sb'),stp=d.querySelector('.ss'),think=d.querySelector('.think'),tk=d.querySelector('.tk');
  let gotToken=false,gotThink=false;
  return {
    addStep(p){
      const el=document.createElement('div');el.className='step';
      const src=(p&&p.source)?String(p.source):'';
      const nm=(src?src.replace(/^agent\//,''):'progress')
        +((p&&p.data&&p.data.step)?(' · step '+p.data.step):'');
      el.innerHTML='<div class="top"><span class="dot run"></span><span class="name">'+esc(nm)+'</span></div>'
        +'<details><summary>载荷</summary><pre>'+esc(json2((p&&p.data)||{}))+'</pre></details>';
      stp.appendChild(el);m.scrollTop=m.scrollHeight;},
    appendToken(text){                     // B5.2 打字机：转义追加，不跑 markdown
      gotToken=true;
      bub.innerHTML+=esc(text||'');
      m.scrollTop=m.scrollHeight;},
    appendThinking(text){                  // B5.2 思考流（D4）：实时追加推理过程
      gotThink=true;
      tk.innerHTML+=esc(text||'');
      m.scrollTop=m.scrollHeight;},
    finish(text,ev){
      // 思考区：若从未收到思考帧则移除（避免空折叠块），否则收敛后折叠
      if(!gotThink&&think&&think.parentNode){think.parentNode.removeChild(think);}
      else if(think){think.open=false;}
      // 若已逐片打出，则仅用最终文本跑一次 md 覆盖渲染，去重（文字不出现两遍）
      bub.innerHTML=md(text||'（无回复）');
      if(ev){stp.innerHTML+=stepRow('narrate','evidence.json',(ev.tokens_out>0)?'ok':'err',
        json2({tokens_in:ev.tokens_in,tokens_out:ev.tokens_out}));}
      m.scrollTop=m.scrollHeight;},
    cancel(){                             // 保留已打出部分，仅追加标注
      bub.innerHTML+='<span class="cancelled">（已取消）</span>';
      m.scrollTop=m.scrollHeight;}
  };}
async function send(){
  if(_streaming&&_ctrl){_ctrl.abort();return;}
  const txt=$('#txt').value.trim();if(!txt)return;
  addMsg('user',txt);$('#txt').value='';
  _streaming=true;_ctrl=new AbortController();
  $('#send').disabled=false;$('#send').textContent='停止';
  const box=addStreamMsg();
  try{
    const r=await fetch('/api/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:txt}),signal:_ctrl.signal});
    if(!r.ok||!r.body)throw new Error('HTTP '+r.status);
    const reader=r.body.getReader(),dec=new TextDecoder();let buf='';
    for(;;){
      const {done,value}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      let i;
      while((i=buf.indexOf('\n\n'))>=0){
        const line=buf.slice(0,i);buf=buf.slice(i+2);
        if(!line.startsWith('data:'))continue;
        let f;try{f=JSON.parse(line.slice(5).trim());}catch(e){continue;}
        const ev=f.event,p=f.payload||{};
        if(ev==='progress'){box.addStep(p);}
        else if(ev==='thinking'){box.appendThinking(p.data?p.data.text:'');}
        else if(ev==='token'){box.appendToken(p.data?p.data.text:'');}
        else if(ev==='final'){box.finish(p.text,p.evidence);}
        else if(ev==='cancelled'){box.cancel();}
        else if(ev==='error'){box.finish('请求失败：'+(p.error||''),null);}
      }
    }
  }catch(e){
    if(e&&e.name==='AbortError'){box.finish('（已取消）',null);}
    else{box.finish('请求失败：'+((e&&e.message)||e),null);}
  }finally{
    _streaming=false;_ctrl=null;
    $('#send').disabled=false;$('#send').textContent='发送';$('#txt').focus();}}
function fmt(x){return x===null||x===undefined?'-':(typeof x==='object'?json2(x):x);}
function tableFrom(rows,cols){return '<table><tr>'+cols.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr>'
  +rows.map(r=>'<tr>'+cols.map(c=>'<td>'+esc(fmt(r[c]))+'</td>').join('')+'</tr>').join('')+'</table>';}
async function loadStatus(){
  try{const j=await (await fetch('/api/status')).json();
    $('#model').textContent=j.provider_label+' · '+j.model;
    $('#session').textContent='session '+j.session_id;
    $('#machine').textContent='v'+j.version+'\n预算 '+(j.budget?j.budget.spent+'/'+j.budget.max:'-');}catch(e){}}
async function loadSession(){
  try{const j=await (await fetch('/api/session')).json();
    $('#messages').innerHTML='';
    (j.history||[]).forEach(m=>addMsg(m.role==='user'?'user':'agent', m.content, null));
  }catch(e){}}
function renderPlugins(rows){
  let h='<h3>插件（agent/plugins · 改后立即重建生效）</h3><table><tr><th>id</th><th>layer</th><th>provides</th><th>版本</th><th>状态</th><th>操作</th></tr>';
  h+=rows.map(p=>{const key=esc(p.name||p.id);let ops='—';
    if(p.delta!=='system/builtin'){
      ops='<button class="chip" data-plug-act="enable" data-key="'+key+'" data-enable="'+(p.desired?'false':'true')+'">'+(p.desired?'停用':'启用')+'</button> '
        +'<button class="chip" data-plug-act="remove" data-key="'+key+'" style="color:var(--err)">移除</button>';}
    let st,cls;
    if(p.delta==='system/builtin'){st='系统/内置';cls='ok';}
    else if(p.desired&&p.actual){st='已启用';cls='ok';}
    else if(p.desired&&!p.actual){st='启用中（重启生效）';cls='run';}
    else if(!p.desired&&p.actual){st='未启用（重启生效）';cls='run';}
    else {st='未启用';cls='';}
    return '<tr><td>'+key+'</td><td>'+esc(p.layer||'')+'</td><td>'+esc((p.provides||[]).join(','))+'</td><td>'+esc(p.version||'')+'</td>'
      +'<td><span class="tag '+cls+'">'+esc(st)+'</span></td><td>'+ops+'</td></tr>';}).join('');
  h+='</table><div class="muted" style="margin-top:6px;font-size:11.5px">新增插件：填名字与 provides → 新增插件（自动启用并重建生效）；系统/内置业务不可管理（与引擎同语义）。</div>';
  $('#pluginsBox').innerHTML=h;
}
async function load(name){
  try{const j=await (await fetch('/api/'+name)).json();
    if(name==='events'){
      const rows=(j.rows||[]).filter(r=>{
        const f=(evFilter==='all');if(f)return true;return (r.event||'').startsWith(evFilter);});
      $('#eventsBox').innerHTML=(rows.length?tableFrom(rows,['seq','event','at','payload']):'<div class="empty">无匹配事件</div>');}
    else if(name==='perms'){
      const jj=j.policy||{};
      $('#permsBox').innerHTML='<h3>权限矩阵（config.agent.permissions，默认 open）</h3>'
        +'<div class="card"><pre class="json">'+esc(json2(jj))+'</pre></div>'
        +'<h3>最近裁决（agent/permission/decision，durable）</h3>'
        +(j.recent&&j.recent.length?tableFrom(j.recent.slice(0,20),['seq','payload','at']):'<div class="empty">暂无裁决</div>');}
        else if(name==='plugins'){ renderPlugins(j.plugins||[]); }
    else if(name==='memory'){
      const lt=j.long_term||{};
      let h='<h3>记忆 · 分层总览</h3>'
        +'<div class="muted" style="margin:-4px 0 12px">三层记忆（系统 / 短期 / 工作）始终可见，无论长期记忆是否启用；长期记忆（plugins.memory）为可选子块。</div>';
      (j.layers||[]).forEach(L=>{
        h+='<div class="card" style="margin-bottom:12px"><div style="display:flex;justify-content:space-between;align-items:center">'
          +'<b>'+esc(L.name)+'</b><span class="tag '+(L.available?'blue':'off')+'">'+(L.available?'已装配':'未启用')+'</span></div>'
          +'<div class="muted" style="font-size:11px;margin:2px 0 8px">'+esc(L.subtitle)+'</div>';
        if(!L.available){ h+='<div class="empty">该层未启用 —— 在「插件」页点「启用」后生效。</div>'; }
        else{
          const s=L.summary||{};
          if(L.key==='system_memory'){
            h+='<div class="mono muted">字符数 '+(s.chars||0)+'</div>';
            if(s.preview) h+='<pre class="json" style="max-height:160px;overflow:auto">'+esc(s.preview)+'</pre>';
          } else if(L.key==='short_term'){
            h+='<span class="tag blue" style="margin:0 6px 4px 0;display:inline-block">消息 '+(s.messages||0)+'</span>'
              +'<span class="tag blue" style="margin:0 6px 4px 0;display:inline-block">已摘要覆盖至 seq '+(s.summary_covers_seq||0)+'</span>'
              +'<span class="tag '+(s.has_summary?'blue':'off')+'" style="margin:0 6px 4px 0;display:inline-block">'+(s.has_summary?'有滚动摘要':'无滚动摘要')+'</span>';
            if(s.compact_stats) h+='<div class="mono muted" style="margin-top:4px">'+esc(json2(s.compact_stats))+'</div>';
            if(s.latest_summary_preview) h+='<pre class="json" style="max-height:140px;overflow:auto">'+esc(s.latest_summary_preview)+'</pre>';
          } else if(L.key==='work_memory'){
            h+='<span class="tag blue" style="display:inline-block">活跃任务 '+(s.count||0)+'</span>';
            if(s.active_tasks&&s.active_tasks.length) h+='<div class="mono" style="margin-top:4px">'+esc(json2(s.active_tasks))+'</div>';
            else h+='<div class="empty" style="margin-top:4px">当前无活跃任务</div>';
          }
        }
        h+='</div>';
      });
      h+='<h3 style="margin-top:18px">长期记忆（plugins.memory · opt-in）</h3>';
      if(!lt.enabled){ h+='<div class="empty">未启用 —— 在「插件」页点「启用」后，下方显示其 Zep 式记忆内容。</div>'; }
      else{
        const st=lt.stats||{};
        h+='<div class="card">'+(Object.entries(st.by_kind||{}).map(([k,v])=>'<span class="tag blue" style="margin:0 6px 4px 0;display:inline-block">'+esc(k)+' '+v+'</span>').join('')||'<span class="muted">空</span>')+'<span class="mono muted" style="margin-left:10px">total='+(st.total||0)+'</span></div>';
        h+=(lt.rows&&lt.rows.length?tableFrom(lt.rows,['id','kind','content','score_observed','created_at','accessed_count','invalid_at','source']):'<div class="empty">暂无记忆</div>');
      }
      $('#memoryBox').innerHTML=h;}
    else if(name==='tools'){
      const rows=j.tools||[];
      let h='<h3>工具目录（ctx.tasks.catalog → 供 tool_call 调用）</h3>';
      h+=rows.length?rows.map(t=>'<div class="card"><div style="display:flex;justify-content:space-between;gap:10px"><b>'+esc(t.name)+'</b><span class="tag blue">'+esc(t.owner||'-')+'</span></div>'
        +'<div class="muted" style="margin-top:4px">'+esc(t.description||'')+'</div>'
        +'<div class="mono" style="margin-top:4px">params='+esc(json2(t.params||{}))+' · required='+esc((t.required||[]).join(','))+'</div></div>').join('')
        :'<div class="empty">暂无工具（接入 windows-mcp 等工具插件后出现）</div>';
      $('#toolsBox').innerHTML=h;}
    else if(name==='settings'){ renderSettings(j); }
  }catch(e){$('#'+name+'Box').innerHTML='<div class="card e">'+esc(e.message)+'</div>';}}
let evFilter='all';
let plugBusy=false;
async function plugAction(payload){
  if(plugBusy)return;plugBusy=true;
  // 立即禁用所有插件按钮并显示“处理中…”，消除“点击后没反应”的观感（重建容器本身需要数秒）
  document.querySelectorAll('#pluginsBox [data-plug-act]').forEach(b=>{b.disabled=true;b.dataset.lb=b.textContent;b.textContent='处理中…';});
  try{
    const r=await fetch('/api/plugins/'+payload.act,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload.body||{})});
    const j=await r.json();
    if(j.plugins) renderPlugins(j.plugins); else await load('plugins');
    if(!j.ok||j.rebuild_error){alert('注意：'+(j.rebuild_error||j.error||r.status)+' （期望态已保存，实际装配可能需重启）');}
    // 后台重建完成后再刷新一次，使状态从“启用中（重启生效）”更新为“已启用”
    setTimeout(()=>{ load('plugins'); loadStatus(); }, 4000);
  }catch(e){alert('请求失败：'+e.message);}finally{plugBusy=false;await loadStatus();}
}
$('#pluginsBox').addEventListener('click',e=>{
  const b=e.target.closest('[data-plug-act]');if(!b)return;
  const act=b.dataset.plugAct, key=b.dataset.key;
  if(act==='remove'&&!confirm('移除 '+key+' ？'))return;
  if(act==='enable'&&b.dataset.enable==='false'&&!confirm('确定停用 '+key+' ？\n停用后需重启服务才会彻底卸载，期间仍装配于当前进程。'))return;
  plugAction(act==='enable'
    ?{act:'enable',body:{id:key,enable:b.dataset.enable==='true'}}
    :{act:'remove',body:{id:key}});
});
function renderSettings(j){
  const box=$('#settingsBox');
  if(j.context_plugin_enabled!==true){
    box.innerHTML='<h3>上下文管理</h3><div class="card muted">上下文管理插件（plugins.context）未启用 —— 在「插件」页点「启用」后，可在此调整参数。</div>';return;}
  const v=j.values||{};
  const fields=[
    ['compact_trigger_tokens','压缩触发阈值 (token)','未摘要历史达到此值即压缩（压缩时机由该阈值单独决定）'],
    ['max_context_tokens','上下文窗口上限 (token)','治理器输入窗口总预算上限'],
    ['reserve_output_tokens','输出余量 (token)','预留给模型输出的 token，不计入输入预算'],
    ['memory_ratio','记忆块占比','长期记忆注入配额占输入预算的比例 (0~1)'],
    ['history_ratio','历史窗口占比','历史消息窗口占输入预算的比例 (0~1)'],
    ['source_clip_tokens','MCP 源头护栏 (token)','外部源头内容的最大裁剪上限'],
    ['clip_max_tokens','循环内 · 单条观察上限 (token)','多步 ReAct 循环内，单条工具观察超过此值即做首尾折叠'],
    ['clip_head_tokens','循环内 · 折叠保留头部 (token)','折叠后保留开头部分（含关键结论/参数）'],
    ['clip_tail_tokens','循环内 · 折叠保留尾部 (token)','折叠后保留结尾部分（错误码通常在尾部）'],
    ['react_keep_recent','循环内 · 最近保留条数','最近 N 条推理链保留原文，更早的才折叠'],
    ['react_min_chain_tokens','循环内 · 链保底额度 (token)','装配会话历史时为多步循环预留的额度，防止循环无空间'],
  ];
  const isFloat=k=>(k==='memory_ratio'||k==='history_ratio');
  let h='<h3>上下文管理 <span class="tag blue">plugins.context 已启用</span></h3>'
    +'<div class="card"><div class="muted" style="margin-bottom:10px">启用上下文管理插件时可调；保存后立即重建容器生效。覆盖写入 data/agent_plugin_config.json，重启后保持。</div>';
  h+=fields.map(([k,label,hint])=>'<label style="display:block;margin:12px 0 0"><div style="font-weight:600;margin-bottom:3px">'+esc(label)+'</div>'
    +'<input id="set_'+k+'" type="number" step="'+(isFloat(k)?'0.01':'1')+'" value="'+esc(v[k])+'" style="width:240px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit">'
    +'<div class="muted" style="font-size:11px;margin-top:3px">'+esc(hint)+'</div></label>').join('');
  h+='<div style="margin-top:16px"><button class="btn" id="saveSettings">保存</button> '
    +'<button class="btn ghost" id="resetSettings">恢复默认</button> <span class="muted" id="setMsg"></span></div></div>';
  box.innerHTML=h;
  $('#saveSettings').onclick=saveSettings;
  $('#resetSettings').onclick=resetSettings;
}
async function saveSettings(){
  const isFloat=k=>(k==='memory_ratio'||k==='history_ratio');
  const keys=['compact_trigger_tokens','max_context_tokens','reserve_output_tokens','memory_ratio','history_ratio','source_clip_tokens','clip_max_tokens','clip_head_tokens','clip_tail_tokens','react_keep_recent','react_min_chain_tokens'];
  const patch={};
  for(const k of keys){const el=$('#set_'+k);if(el&&el.value!==''){patch[k]=isFloat(k)?parseFloat(el.value):parseInt(el.value,10);}}
  $('#setMsg').textContent='保存中…';
  try{const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({patch})});
    const j=await r.json();if(!j.ok){$('#setMsg').textContent='失败：'+(j.error||r.status);return;}
    $('#setMsg').textContent='已保存并重建生效 ✓';renderSettings(j.settings);}
  catch(e){$('#setMsg').textContent='失败：'+e.message;}
}
async function resetSettings(){
  if(!confirm('恢复上下文管理默认参数？当前覆盖将被清除。'))return;
  $('#setMsg').textContent='恢复中…';
  try{const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reset:true})});
    const j=await r.json();if(!j.ok){$('#setMsg').textContent='失败：'+(j.error||r.status);return;}
    $('#setMsg').textContent='已恢复默认 ✓';renderSettings(j.settings);}
  catch(e){$('#setMsg').textContent='失败：'+e.message;}
}
document.addEventListener('DOMContentLoaded',()=>{
  $('#chips').innerHTML=chips.map(c=>'<button class="chip">'+c+'</button>').join('');
  loadStatus();loadSession();
  $('#newSession').onclick=async()=>{await fetch('/api/session/reset',{method:'POST'});$('#messages').innerHTML='';loadSession();};
  $('#send').onclick=send;
  $('#txt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
  $('#chips').addEventListener('click',e=>{if(e.target.classList.contains('chip')){$('#txt').value=e.target.textContent;send();}});
  document.querySelectorAll('.nav-item').forEach(b=>b.onclick=()=>{
    document.querySelectorAll('.nav-item').forEach(x=>x.classList.remove('active'));b.classList.add('active');
    const vn=b.dataset.view;
    document.querySelectorAll('section[data-view]').forEach(s=>s.classList.toggle('active',s.dataset.view===vn));
    if(vn!=='chat')load(vn);});
  $('#evFilters').addEventListener('click',e=>{
    if(!e.target.classList.contains('chip'))return;
    document.querySelectorAll('#evFilters .chip').forEach(x=>x.classList.remove('active'));
    e.target.classList.add('active');evFilter=e.target.dataset.ev;load('events');});
  $('#pscaffold').onclick=async()=>{
    const name=$('#pname').value.trim();if(!name)return alert('填插件名');
    const provides=$('#pprovides').value.split(',').map(s=>s.trim()).filter(Boolean);
    plugAction({act:'scaffold',body:{name,provides:provides.length?provides:null}});
    $('#pname').value='';$('#pprovides').value='';};
  load('perms');load('plugins');load('memory');load('tools');load('settings');
});
</script></body></html>
"""


async def _serve(args: argparse.Namespace) -> int:
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

    c, pm = await _boot(args)

    class _Box:
        """可换容器的句柄：handlers 引用 c.root 不受「重建容器」影响。"""

        def __init__(self, c):
            self.set(c)

        def set(self, c):
            self._c = c

        @property
        def root(self):
            return self._c.root

        @property
        def container(self):
            return self._c

        async def teardown(self):
            await self._c.teardown()

    c = _Box(c)
    session_id = uuid.uuid4().hex[:8]
    # B6/B5 共用：run_id -> 在跑的任务。取消端点据此 cancel；断连时清理，防孤儿任务。
    active_runs: dict[str, asyncio.Task] = {}

    def _sse(event: str, payload: dict) -> str:
        """一帧 SSE（data: {...}\\n\\n）。帧类型见设计方案 §4.5。"""
        return "data: " + json.dumps({"event": event, "payload": payload or {}},
                                     ensure_ascii=False) + "\n\n"

    async def _apply_rebuild() -> None:
        """按当前 config（含用户覆盖）+ 当前插件期望态重建容器（管理操作即时生效）。"""
        old = c._c
        c2 = build_agent(config=_merge_cfg(args), plugin_mgr=pm)
        await c2.assemble()
        await c2.start()
        try:
            await old.teardown()
        except Exception:  # noqa: BLE001
            pass
        c.set(c2)

    app = FastAPI(title="Agent Harness", docs_url=None, redoc_url=None)

    @app.get("/")
    async def _home():
        # no-store：前端改版后，防止浏览器用启发式缓存继续喂旧版 JS（曾导致"看起来没有流式"）
        return HTMLResponse(_PAGE, headers={"Cache-Control": "no-store"})

    @app.post("/api/chat")
    async def _chat(body: dict):
        try:
            reply = await c.root.chat.send(c.root, str(body.get("message") or ""),
                                           session_id=session_id)
            return JSONResponse({"text": reply.text, "ok": reply.ok, "error": reply.error,
                                 "evidence": {"plan": reply.plan, "evidence": reply.evidence,
                                              "tokens_in": reply.tokens_in,
                                              "tokens_out": reply.tokens_out}})
        except Exception as exc:  # noqa: BLE001 —— 回显给前端，不裸崩
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.post("/api/chat/stream")
    async def _chat_stream(body: dict):
        """B5 流式：SSE 逐帧推送（start → progress… → final / error / cancelled）。

        与 B6 共用同一 run 生命周期：本端点持有任务并登记 run_id，取消端点据此 cancel。
        ``/api/chat`` 保留不动（--once 与离线测试依赖它）。
        """
        message = str(body.get("message") or "")
        run_id = uuid.uuid4().hex[:8]
        q: asyncio.Queue = asyncio.Queue()

        async def _worker() -> None:
            try:
                reply = await c.root.chat.send(c.root, message, session_id=session_id)
                await q.put({"event": "final", "payload": {
                    "text": reply.text, "ok": reply.ok, "error": reply.error,
                    "evidence": {"plan": reply.plan, "evidence": reply.evidence,
                                 "tokens_in": reply.tokens_in, "tokens_out": reply.tokens_out}}})
            except asyncio.CancelledError:
                await q.put({"event": "cancelled", "payload": {"run_id": run_id}})
                raise                                  # 保持取消语义，不吞
            except Exception as exc:                   # noqa: BLE001
                await q.put({"event": "error", "payload": {"error": str(exc)}})

        async def _gen():
            task: asyncio.Task | None = None
            try:
                yield _sse("start", {"run_id": run_id})
                # 先订阅（不漏帧），再起任务
                async with tap_progress(c.root, queue=q):
                    task = asyncio.create_task(_worker())
                    active_runs[run_id] = task
                    try:
                        while True:
                            frame = await q.get()
                            yield _sse(frame["event"], frame["payload"])
                            if frame["event"] in ("final", "error", "cancelled"):
                                return
                    finally:
                        active_runs.pop(run_id, None)
                        if task is not None and not task.done():
                            task.cancel()              # 断连/提前退出 → 取消，防孤儿
            except asyncio.CancelledError:
                return                                 # 客户端断连：不逃逸，任务已在 finally 取消
            except Exception as exc:                   # noqa: BLE001
                yield _sse("error", {"error": str(exc)})

        return StreamingResponse(_gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.post("/api/chat/cancel")
    async def _chat_cancel(body: dict):
        """B6 取消：按 run_id 取消在跑的那轮（`asyncio.Task.cancel()`，任意 await 点即时生效）。"""
        rid = str(body.get("run_id") or "")
        task = active_runs.get(rid)
        if task is None:
            return JSONResponse({"ok": False, "error": "run_id 不存在或该轮已结束"},
                                status_code=404)
        task.cancel()
        return JSONResponse({"ok": True, "run_id": rid})

    @app.get("/api/status")
    async def _status() -> dict:
        svc = c.root.get(Services.LLM)
        budget_cfg = ((c.root.config or {}).get("agent") or {}).get("budget") or {}
        return {"provider_label": getattr(svc, "provider_label", "?"),
                "model": getattr(svc, "model", "?"),
                "version": __import__("agent").__version__,
                "session_id": session_id,
                "budget": {"spent": c.root.state.get(StateKeys.BUDGET_SPENT, 0),
                           "max": budget_cfg.get("max_tokens_per_run", 0)}}

    @app.get("/api/events")
    async def _events() -> dict:
        rows = await c.root.db.query(
            c.root, "SELECT seq, event, payload, at FROM agent_events ORDER BY seq DESC LIMIT 120")
        return {"rows": rows}

    @app.get("/api/perms")
    async def _perms() -> dict:
        recent = await c.root.db.query(
            c.root, "SELECT seq, payload, at FROM agent_events"
                    " WHERE event='agent/permission/decision' ORDER BY seq DESC LIMIT 30")
        return {"policy": c.root.guard.policy(c.root), "recent": recent}

    @app.get("/api/session")
    async def _session() -> dict:
        return {"session_id": session_id,
                "history": await c.root.chat.history(c.root, session_id)}

    @app.post("/api/session/reset")
    async def _session_reset():
        cleared = await c.root.chat.reset(c.root, session_id)
        return JSONResponse({"ok": True, "cleared": cleared})

    @app.get("/api/tools")
    async def _tools() -> dict:
        return {"tools": c.root.tasks.catalog()}

    # ---- 记忆（分层总览：系统 / 短期 / 工作 + 长期子块） ----
    # 解耦纪律：消费面只用 ctx.has(Services.X) 守卫 + 门面方法，不 import 任何插件内部模块。

    @app.get("/api/memory")
    async def _memory() -> dict:
        layers: list[dict] = []

        # 第1层 系统记忆（SYSTEM_MEMORY 提供的是纯文本字符串，无门面对象）
        if c.root.has(Services.SYSTEM_MEMORY):
            sm_text = c.root.get(Services.SYSTEM_MEMORY) or ""
            layers.append({"key": "system_memory", "name": "系统记忆",
                           "subtitle": "第1层 · 身份/边界叙事（启动全量注入）",
                           "available": True,
                           "summary": {"chars": len(sm_text),
                                       "preview": (sm_text[:240] + "…") if len(sm_text) > 240 else sm_text}})
        else:
            layers.append({"key": "system_memory", "name": "系统记忆",
                           "subtitle": "第1层 · 身份/边界叙事",
                           "available": False, "summary": {}})

        # 第2层 短期记忆（SHORT_TERM 门面 ShortTermMemory）
        if c.root.has(Services.SHORT_TERM):
            short = c.root.get(Services.SHORT_TERM)
            try:
                cnt = await short.count(c.root, session_id)
                meta = await short.summary_meta(c.root, session_id)
                covers = int(meta["covers_up_to_seq"]) if meta else 0
                latest = await short.latest_summary(c.root, session_id)
                st_stats = short.stats()
            except Exception:  # noqa: BLE001 —— 单窗诊断失败不影响整体展示
                cnt, covers, latest, st_stats = 0, 0, None, {}
            layers.append({"key": "short_term", "name": "短期记忆",
                           "subtitle": "第2层 · 会话转写 + 滚动摘要（当前会话）",
                           "available": True,
                           "summary": {"messages": cnt, "summary_covers_seq": covers,
                                       "has_summary": bool(latest),
                                       "latest_summary_preview": ((latest[:240] + "…") if latest and len(latest) > 240 else (latest or "")),
                                       "compact_stats": st_stats}})
        else:
            layers.append({"key": "short_term", "name": "短期记忆",
                           "subtitle": "第2层 · 会话转写 + 滚动摘要",
                           "available": False, "summary": {}})

        # 第3层 工作记忆（WORK_MEMORY 门面 WorkMemory）
        if c.root.has(Services.WORK_MEMORY):
            wm = c.root.get(Services.WORK_MEMORY)
            try:
                active = await wm.list_active(session_id)
            except Exception:  # noqa: BLE001
                active = []
            layers.append({"key": "work_memory", "name": "工作记忆",
                           "subtitle": "第3层 · 任务级进度缓存（Redis / 降级内存）",
                           "available": True,
                           "summary": {"active_tasks": active, "count": len(active)}})
        else:
            layers.append({"key": "work_memory", "name": "工作记忆",
                           "subtitle": "第3层 · 任务级进度缓存",
                           "available": False, "summary": {}})

        # 长期记忆（MEMORY 目录插件，opt-in）：独立子块，不影响三层展示
        long_term = {"enabled": c.root.has(Services.MEMORY), "stats": {}, "rows": []}
        if c.root.has(Services.MEMORY):
            try:
                long_term["stats"] = await c.root.memory.stats(c.root)
                long_term["rows"] = await c.root.memory.list_recent(c.root, limit=40)
            except Exception:  # noqa: BLE001
                long_term["stats"], long_term["rows"] = {"error": "stats failed"}, []

        return {"layers": layers, "long_term": long_term}

    # ---- 插件管理（引擎式：desired/actual/delta；改动即时重建生效） ----

    @app.get("/api/plugins")
    async def _plugins() -> dict:
        manifest = c.container.plugin_manifest()
        statuses = await c.container.plugin_statuses()
        status_map = {s["id"]: s.get("status") for s in statuses}
        discovered = pm.discovered()
        enabled = set(pm.enabled())
        assembled = {m["id"] for m in manifest}
        discovered_ids = {d["name"] for d in discovered}
        rows: list[dict] = []
        for d in discovered:
            e, a = d["name"] in enabled, d["name"] in assembled
            delta = ("enabled · assembled" if (e and a) else
                     ("enabled · 待重启装配" if (e and not a) else
                      ("disabled · 未装配" if (not e and not a) else "待停用")))
            # 行结构统一：目录插件与内置 manifest 两种来源都同时给 id + name（前端按键取 id）
            rows.append({**d, "id": d["name"], "desired": e, "actual": a, "delta": delta,
                         "status": status_map.get(d["name"])})
        for m in manifest:
            # 含 system 层插件一同展示（只读：前端对 delta=="system/builtin" 隐藏操作按钮）。
            # 仅按 discovered 去重，避免目录插件与其 manifest 项重复出现。
            if m["id"] in discovered_ids:
                continue
            rows.append({**m, "name": m["id"], "desired": True, "actual": True,
                         "delta": "system/builtin",
                         "status": status_map.get(m["id"])})
        return {"plugins": rows, "state_exists": pm.state_file.exists()}

    async def _bg_rebuild() -> None:
        """后台重建容器（不阻塞管理请求）。失败仅记录，不回滚期望态。"""
        try:
            await _apply_rebuild()
        except Exception as exc:  # noqa: BLE001
            print(f"[rebuild] 后台重建失败（期望态已保存，实际装配可能需重启）：{exc}",
                  file=sys.stderr)

    async def _mutate_then_fetch(background: bool = False) -> dict:
        # 先返回期望态已更新的当前快照（desired 已写入 state.json）。
        # 容器重建代价高（含 MCP 子进程拉起等，常需数十秒），故改为后台执行，
        # 避免管理请求长时间阻塞、前端“点了没反应”。前端会显示“处理中…”并在稍后自动刷新。
        res = await _plugins()
        if background:
            asyncio.create_task(_bg_rebuild())
            return res
        try:
            await _apply_rebuild()
            res = await _plugins()
        except Exception as exc:  # noqa: BLE001
            res = {**res, "rebuild_error": f"重建失败（期望态已保存，实际装配可能需重启）：{exc}"}
        return res

    @app.post("/api/plugins/enable")
    async def _plugin_enable(body: dict):
        pm.set_enabled(str(body.get("id") or ""), bool(body.get("enable", True)))
        # 无论重建成功与否都返回最新插件状态（含 plugins），保证前端即时刷新表格
        res = await _mutate_then_fetch(background=True)
        return JSONResponse({"ok": True, **res})

    @app.post("/api/plugins/scaffold")
    async def _plugin_scaffold(body: dict):
        info = pm.scaffold(str(body.get("name") or ""), body.get("provides") or None)
        res = await _mutate_then_fetch(background=True)
        return JSONResponse({"ok": True, "info": info, **res})

    @app.post("/api/plugins/remove")
    async def _plugin_remove(body: dict):
        info = pm.remove(str(body.get("id") or ""))
        res = await _mutate_then_fetch(background=True)
        return JSONResponse({"ok": True, "info": info, **res})

    # ---- 设置：上下文管理（前端「设置」页；覆盖持久化 + 即时重建生效） ----

    @app.get("/api/settings")
    async def _settings() -> dict:
        ctx_enabled = "plugins.context" in set(pm.enabled())
        ov = _load_overrides()
        st = ov.get("plugins.short_term", {}) or {}
        cx = ov.get("plugins.context", {}) or {}
        values: dict[str, int | float] = {}
        for k in _SETTING_SCHEMA:
            try:
                values[k] = _coerce_setting(k, cx.get(k, st.get(k, _SETTING_DEFAULTS[k])))
            except (ValueError, TypeError):
                values[k] = _SETTING_DEFAULTS[k]
        return {"context_plugin_enabled": ctx_enabled, "values": values}

    @app.post("/api/settings")
    async def _settings_update(body: dict):
        try:
            if bool(body.get("reset")):
                ov = _load_overrides()
                for pid in ("plugins.context", "plugins.short_term"):
                    ov.pop(pid, None)
                _save_overrides(ov)
                await _apply_rebuild()
                return JSONResponse({"ok": True, "settings": await _settings()})
            patch = body.get("patch") or {}
            if not isinstance(patch, dict) or not patch:
                return JSONResponse({"ok": False, "error": "patch 为空"}, status_code=400)
            buckets: dict[str, dict] = {}
            for k, v in patch.items():
                if k not in _SETTING_SCHEMA:
                    return JSONResponse({"ok": False, "error": f"未知设置项: {k}"}, status_code=400)
                coerced = _coerce_setting(k, v)
                buckets.setdefault(_PLUGIN_OF_KEY[k], {})[k] = coerced
            ov = _load_overrides()
            for pid, b in buckets.items():
                cur = dict(ov.get(pid, {}))
                cur.update(b)
                ov[pid] = cur
            _save_overrides(ov)
            await _apply_rebuild()
            return JSONResponse({"ok": True, "settings": await _settings()})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "error": f"参数错误: {exc}"}, status_code=400)

    url = f"http://{args.host}:{args.port}"
    print(f"Agent Harness 就绪: {url}   （Ctrl+C 退出）", file=sys.stderr)
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    task = asyncio.create_task(server.serve())
    try:
        await task
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await c.teardown()
    return 0


async def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    if args.once:
        return await _once(args)
    return await _serve(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))