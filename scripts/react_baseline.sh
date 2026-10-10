#!/usr/bin/env bash
# P0-1 基线采集 —— React 收敛度量（只读 SQL，不改任何数据）
#
# 用法：
#   1) 服务跑着 → 在页面 http://127.0.0.1:8720/ 上跑 5~10 个复杂任务
#   2) 跑完本脚本 → 得到四个基线数字
#
# 口径说明（与主方案 §5 一致）：
#   - agent/react/exhausted 与 agent/react/final 都落 agent_events，无需新埋点
#   - REACT_STEP* / step|start|end 只 emit 不落库 ⇒ 步数要从 exhausted/final 的 payload 取
#   - 委派触发率 = SUBAGENT_SPAWN / REACT_START

cd "$(dirname "$0")/.." || exit 1
PY=<用户主目录>/.workbuddy/binaries/python/versions/3.13.12.old.27804/python.exe

# ★ 库选择：真实运行数据在 **agent_chat.db**（chat/前端那条链）；agent.db 是
#   早期遗留、几乎无数据（曾误查它导致"只有 1 条"的假象）。可用 DB 环境变量覆盖。
DB="${AGENT_DB:-data/agent_chat.db}"
echo "数据源：$DB"
echo

$PY - "$DB" <<'EOF'
import sqlite3, json, sys

c = sqlite3.connect(sys.argv[1])
c.row_factory = sqlite3.Row

def rows(sql, args=()):
    try:
        return c.execute(sql, args).fetchall()
    except Exception as e:
        print(f"  [查询失败] {e}")
        return []

print("=" * 60)
print("① 收敛率：触顶 vs 正常收敛")
print("=" * 60)
r = rows("""SELECT CASE WHEN event='agent/react/exhausted' THEN 'exhausted'
                  WHEN event='agent/react/final'     THEN 'converged' END kind,
             COUNT(*) n FROM agent_events
            WHERE event IN ('agent/react/exhausted','agent/react/final')
            GROUP BY kind""")
tot = sum(x["n"] for x in r)
if not tot:
    print("  （无数据 —— 先去页面跑几个复杂任务）")
else:
    for x in r:
        print(f"  {x['kind']:<10} {x['n']:>4}  {x['n']/tot*100:5.1f}%")

print()
print("=" * 60)
print("② 步数分布（steps 越小越健康；看触顶任务是不是卡在 8）")
print("=" * 60)
r = rows("""SELECT event, json_extract(payload,'$.steps') s
            FROM agent_events
            WHERE event IN ('agent/react/exhausted','agent/react/final')
              AND json_extract(payload,'$.steps') IS NOT NULL""")
if not r:
    print("  （payload 无 steps 字段或无数据）")
else:
    for ev in ("agent/react/final", "agent/react/exhausted"):
        v = sorted(x["s"] for x in r if x["event"] == ev)
        if not v:
            continue
        avg = sum(v) / len(v)
        med = v[len(v)//2]
        print(f"  {ev.split('/')[-1]:<10} n={len(v):<3} 平均={avg:.1f} 中位={med} 区间=[{v[0]}..{v[-1]}]")

print()
print("=" * 60)
print("③ 触顶原因分布（决定该做 P1-3 还是 P1-4）")
print("=" * 60)
r = rows("""SELECT CASE WHEN json_extract(payload,'$.reason') LIKE '已达步数上限%' THEN 'step_limit'
                  WHEN json_extract(payload,'$.reason') LIKE '%无进展%'           THEN 'no_progress'
                  WHEN json_extract(payload,'$.reason') LIKE '%超时%'             THEN 'timeout'
                  WHEN json_extract(payload,'$.reason') LIKE '%推理失败%'         THEN 'infer_error'
                  WHEN json_extract(payload,'$.reason') LIKE '%模型调用被拒%'     THEN 'guard_budget'
                  WHEN json_extract(payload,'$.cancelled') = 1                    THEN 'cancelled'
                  ELSE 'other' END cause, COUNT(*) n
             FROM agent_events WHERE event='agent/react/exhausted' GROUP BY cause""")
if not r:
    print("  （无触顶记录 —— 好消息，说明还没撞到 8 步上限）")
else:
    for x in sorted(r, key=lambda y: -y["n"]):
        print(f"  {x['cause']:<14} {x['n']:>4}")

print()
print("=" * 60)
print("④ 委派触发率（SPAWN / START）")
print("=" * 60)
starts = rows("SELECT COUNT(*) n FROM agent_events WHERE event='agent/react/start'")
spawns = rows("SELECT COUNT(*) n FROM agent_events WHERE event='agent/subagent/spawn'")
ns = starts[0]["n"] if starts else 0
np_ = spawns[0]["n"] if spawns else 0
print(f"  react/start={ns}  subagent/spawn={np_}"
      + (f"  触发率={np_/ns*100:.1f}%" if ns else "  （无 run 记录）"))

print()
print("=" * 60)
print("⑤ 工具调用量（看是否值得并发化 P1-3）")
print("=" * 60)
r = rows("""SELECT COUNT(*) n FROM agent_events WHERE event='agent/tasks/post-execute'""")
print(f"  tasks/post-execute = {r[0]['n'] if r else 0}")

print()
print("=" * 60)
print("⑥ 无进展检测命中数（P1-1 是否在真模型上起作用）")
print("=" * 60)
r = rows("""SELECT COUNT(*) n FROM agent_events
            WHERE event='agent/react/exhausted'
              AND json_extract(payload,'$.reason') LIKE '%无进展%'""")
print(f"  no_progress 收尾 = {r[0]['n'] if r else 0}")
c.close()
EOF
