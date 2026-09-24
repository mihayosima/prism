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

from agent.api import Services
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


async def _boot(args: argparse.Namespace):
    from agent.sys.plugin_manager import PluginManager

    pm = PluginManager(plugin_dir="agent/plugins", state_file="data/agent_plugin_state.json")
    c = build_agent(config=_cfg(args), plugin_mgr=pm)
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
  d.innerHTML='<div class="who">Agent</div><div class="bubble sb">…</div><div class="steps ss"></div>';
  $('#messages').appendChild(d);
  const m=$('.messages');m.scrollTop=m.scrollHeight;
  const bub=d.querySelector('.sb'),stp=d.querySelector('.ss');
  let gotToken=false;
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
    finish(text,ev){
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
    else if(name==='plugins'){
      const rows=j.plugins||[];
      let h='<h3>插件（agent/plugins · 改后立即重建生效）</h3><table><tr><th>id</th><th>layer</th><th>provides</th><th>版本</th><th>状态</th><th>操作</th></tr>';
      h+=rows.map(p=>{const key=esc(p.name||p.id);let ops='—';
        if(p.delta!=='system/builtin'){
          ops='<button class="chip" data-plug-act="enable" data-key="'+key+'" data-enable="'+(p.desired?'false':'true')+'">'+(p.desired?'停用':'启用')+'</button> '
            +'<button class="chip" data-plug-act="remove" data-key="'+key+'" style="color:var(--err)">移除</button>';}
        return '<tr><td>'+key+'</td><td>'+esc(p.layer||'')+'</td><td>'+esc((p.provides||[]).join(','))+'</td><td>'+esc(p.version||'')+'</td>'
          +'<td><span class="tag '+(String(p.delta||'').startsWith('enabled')?'ok':'run')+'">'+esc(p.delta||'')+'</span></td><td>'+ops+'</td></tr>';}).join('');
      h+='</table><div class="muted" style="margin-top:6px;font-size:11.5px">新增插件：填名字与 provides → 新增插件（自动启用并重建生效）；系统/内置业务不可管理（与引擎同语义）。</div>';
      $('#pluginsBox').innerHTML=h;}
    else if(name==='memory'){
      if(j.enabled===false){
        $('#memoryBox').innerHTML='<h3>记忆</h3><div class="empty">记忆插件（plugins.memory）未启用 —— 在「插件」页点「启用」后生效。</div>';return;}
      const st=j.stats||{};
      let h='<h3>记忆（plugins.memory · Zep 式：事实/意图/决策/观察 + 打分衰减）</h3>'
        +'<div class="card">'+(Object.entries(st.by_kind||{}).map(([k,v])=>'<span class="tag blue" style="margin:0 6px 4px 0;display:inline-block">'+esc(k)+' '+v+'</span>').join('')||'<span class="muted">空</span>')+'<span class="mono muted" style="margin-left:10px">total='+(st.total||0)+'</span></div>'
        +(j.rows&&j.rows.length?tableFrom(j.rows,['id','kind','content','score_observed','created_at','accessed_count','invalid_at','source']):'<div class="empty">暂无记忆 —— 与 Agent 对话 / 跑技能后会经 LLM 提取写入</div>');
      $('#memoryBox').innerHTML=h;}
    else if(name==='tools'){
      const rows=j.tools||[];
      let h='<h3>工具目录（ctx.tasks.catalog → 供 tool_call 调用）</h3>';
      h+=rows.length?rows.map(t=>'<div class="card"><div style="display:flex;justify-content:space-between;gap:10px"><b>'+esc(t.name)+'</b><span class="tag blue">'+esc(t.owner||'-')+'</span></div>'
        +'<div class="muted" style="margin-top:4px">'+esc(t.description||'')+'</div>'
        +'<div class="mono" style="margin-top:4px">params='+esc(json2(t.params||{}))+' · required='+esc((t.required||[]).join(','))+'</div></div>').join('')
        :'<div class="empty">暂无工具（接入 windows-mcp 等工具插件后出现）</div>';
      $('#toolsBox').innerHTML=h;}
  }catch(e){$('#'+name+'Box').innerHTML='<div class="card e">'+esc(e.message)+'</div>';}}
let evFilter='all';
let plugBusy=false;
async function plugAction(payload){
  if(plugBusy)return;plugBusy=true;
  try{
    const r=await fetch('/api/plugins/'+payload.act,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload.body||{})});
    const j=await r.json();
    if(!j.ok&&r.status>=400)alert('失败：'+(j.error||r.status));
    await load('plugins');await loadStatus();
  }catch(e){alert('失败：'+e.message);}finally{plugBusy=false;}
}
$('#pluginsBox').addEventListener('click',e=>{
  const b=e.target.closest('[data-plug-act]');if(!b)return;
  const act=b.dataset.plugAct, key=b.dataset.key;
  if(act==='remove'&&!confirm('移除 '+key+' ？'))return;
  plugAction(act==='enable'
    ?{act:'enable',body:{id:key,enable:b.dataset.enable==='true'}}
    :{act:'remove',body:{id:key}});
});
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
  load('perms');load('plugins');load('memory');load('tools');
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
        """按当前 config + 当前插件期望态重建容器（管理操作即时生效）。"""
        old = c._c
        c2 = build_agent(config=dict(old.root.config), plugin_mgr=pm)
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
                "model": getattr(svc, "_model", "?"),
                "version": __import__("agent").__version__,
                "session_id": session_id,
                "budget": {"spent": c.root.state.get("budget_spent", 0),
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

    # ---- 记忆（长期） ----

    @app.get("/api/memory")
    async def _memory() -> dict:
        # 记忆是目录插件（plugins.memory）：未启用 → 友好降级，不裸崩
        if not c.root.has(Services.MEMORY):
            return {"enabled": False, "stats": {"by_kind": {}, "total": 0}, "rows": []}
        stats = await c.root.memory.stats(c.root)
        rows = await c.root.db.query(
            c.root, "SELECT id, kind, content, score_observed, created_at, last_accessed_at,"
                    " accessed_count, invalid_at, source FROM agent_memories"
                    " ORDER BY created_at DESC LIMIT 40")
        return {"enabled": True, "stats": stats, "rows": rows}

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
            if m.get("layer") == "system" or m["id"] in discovered_ids:
                continue
            rows.append({**m, "name": m["id"], "desired": True, "actual": True,
                         "delta": "system/builtin",
                         "status": status_map.get(m["id"])})
        return {"plugins": rows, "state_exists": pm._state_file.exists()}

    async def _mutate_then_fetch() -> dict:
        await _apply_rebuild()
        return await _plugins()

    @app.post("/api/plugins/enable")
    async def _plugin_enable(body: dict):
        try:
            pm.set_enabled(str(body.get("id") or ""), bool(body.get("enable", True)))
            res = await _mutate_then_fetch()
            return JSONResponse({"ok": True, "plugins": res["plugins"]})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.post("/api/plugins/scaffold")
    async def _plugin_scaffold(body: dict):
        try:
            info = pm.scaffold(str(body.get("name") or ""), body.get("provides") or None)
            res = await _mutate_then_fetch()
            return JSONResponse({"ok": True, "info": info, "plugins": res["plugins"]})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.post("/api/plugins/remove")
    async def _plugin_remove(body: dict):
        try:
            info = pm.remove(str(body.get("id") or ""))
            res = await _mutate_then_fetch()
            return JSONResponse({"ok": True, "info": info, "plugins": res["plugins"]})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

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