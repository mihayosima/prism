/**
 * SettingsView 视图逻辑（B2.3 迁移自 legacy app.js:469-575）
 * ------------------------------------------------------------------
 * 拆为「字段描述表（纯数据）」+「HTML 生成（纯函数）」+「补丁收集（纯函数）」，
 * 三者均可独立对拍；交互由 Vue 组件驱动。
 * 时间：2026-09-30 11:40 (GMT+8)
 */

import { esc } from '../components/markdown'
import type { SettingsResponse, SettingsDomainOption, SettingsScope } from '../api/types'

/** 浮点型字段（legacy app.js:514、551 两处硬编码，此处收敛为一处） */
export const FLOAT_KEYS = new Set([
  'memory_ratio',
  'history_ratio',
  'window_high_ratio',
  'window_low_ratio',
  'deadline_s',
])

export const isFloatKey = (k: string): boolean => FLOAT_KEYS.has(k)

/** 上下文管理字段表（legacy app.js:492-511，原样） */
export const CONTEXT_FIELDS: Array<[string, string, string]> = [
  ['compact_trigger_tokens', '压缩触发阈值 (token)', '未摘要历史达到此值即压缩（压缩时机由该阈值单独决定）'],
  ['max_context_tokens', '上下文窗口上限 (token)', '治理器输入窗口总预算上限'],
  ['reserve_output_tokens', '输出余量 (token)', '预留给模型输出的 token，不计入输入预算'],
  ['memory_ratio', '记忆块占比', '长期记忆注入配额占输入预算的比例 (0~1)'],
  ['history_ratio', '历史窗口占比', '历史消息窗口占输入预算的比例 (0~1)'],
  ['source_clip_tokens', 'MCP 源头护栏 (token)', '外部源头内容的最大裁剪上限'],
  ['clip_max_tokens', '循环内 · 单条观察上限 (token)', '多步 ReAct 循环内，单条工具观察超过此值即做首尾折叠'],
  ['clip_head_tokens', '循环内 · 折叠保留头部 (token)', '折叠后保留开头部分（含关键结论/参数）'],
  ['clip_tail_tokens', '循环内 · 折叠保留尾部 (token)', '折叠后保留结尾部分（错误码通常在尾部）'],
  ['react_keep_recent', '循环内 · 最近保留条数', '最近 N 条推理链保留原文，更早的才折叠'],
  ['react_min_chain_tokens', '循环内 · 链保底额度 (token)', '装配会话历史时为多步循环预留的额度，防止循环无空间'],
  ['working_window_tokens', '经济窗口 (token)', '水位判定的分母；与能力上限 max_context_tokens 解耦。默认 96k——保证普通任务永不触发压缩（压缩是超长任务兜底）'],
  ['window_high_ratio', '触发水位 (0~1)', 'load_ratio ≥ 此值即判 high（默认 0.70）'],
  ['window_low_ratio', '回落目标 (0~1)', '水位回落参考值（记录用，默认 0.40）'],
  ['resident_cap_tokens', '常驻层 cap (token)', '系统提示 / 输出格式等常驻块上限；绝对量级（0 = 无 cap）'],
  ['on_demand_cap_tokens', '按需层 cap (token)', '工具 schema / 记忆召回等按需块上限（0 = 无 cap）'],
  ['prompt_cap_tokens', '输入层 cap (token)', '用户本轮输入 / 附件上限（0 = 无 cap）'],
  ['clear_at_least_tokens', '清除最小释放量 (token)', '单次 clear 动作的最小释放量门槛；低于此值不触发清除（对标 Anthropic clear_at_least）'],
]

/** 压缩模式枚举（legacy app.js:512-513） */
export const COMPACT_MODES = ['off', 'shadow', 'on'] as const
export const COMPACT_MODE_LABELS: Record<string, string> = {
  off: '不治理（不做任何动作）',
  shadow: '只算不做：照常计算决策并写遥测，用于校准阈值（推荐）',
  on: '实际执行结构性改变（clear/compact 落位）',
}

/** 审批通道枚举（legacy app.js:529-530） */
export const APPROVAL_PROVIDERS = ['auto-deny', 'auto-allow', 'web', 'cli'] as const
export const APPROVAL_PROVIDER_LABELS: Record<string, string> = {
  'auto-deny': '问人即拒（fail-safe，无弹窗）',
  'auto-allow': '问人即批（开发用，无弹窗）',
  web: '网页弹窗审批（推荐）',
  cli: '终端问答（y=批 / a=本会话总是允许）',
}

/**
 * 沙箱**隔离档位**（2026-03 收口后沙箱只做 confinement，不再判准入）。
 * 取值域与后端 `agent/plugins/sandbox/__init__.py::MODES` 逐字一致 —— 改一处必须改两处。
 */
export const SANDBOX_MODES = ['none', 'read-only', 'workspace-write'] as const
export const SANDBOX_MODE_LABELS: Record<string, string> = {
  none: '不启用隔离（当前唯一能兑现的档位）',
  'read-only': '声明只读（⚠ 当前无进程级隔离可兑现）',
  'workspace-write': '声明工作区可写（⚠ 当前无进程级隔离可兑现）',
}

/**
 * 隔离**强度**自述徽标（对齐 DeepSeek Harness 的 `SandboxEnforcement`）。
 * ★ 关键：`none` 必须**显式显示为警告**，不能显示成"正常"——
 * 插件页此前显示 `enabled · assembled` + `error: null` 看起来健康，是本项目最大的误导源。
 */
export const ENFORCEMENT_BADGES: Record<string, { text: string; color: string; title: string }> = {
  full: { text: '完整隔离', color: 'var(--ok, #16a34a)', title: '后端完整实现了该档位承诺的所有文件隔离' },
  partial: {
    text: '部分隔离',
    color: 'var(--warn, #d97706)',
    title: '因操作系统或内核版本限制只能做到部分隔离（例：Windows ACL 边界）。不可当作 full 使用。',
  },
  none: {
    text: '⚠ 无隔离',
    color: 'var(--danger, #dc2626)',
    title: '无任何进程级隔离。这是事实陈述，不是缺陷陈述 —— 但绝不是"受保护"。',
  },
}

/**
 * 子进程落点块（2026-10-04）。
 *
 * ★ 与上面的强度徽标**刻意分开**，措辞上强调"这不是保护强度"：
 * 落点收窄（cwd + 缓存目录重定向）只让**相对路径**的写入落在会话私有目录，
 * **绝对路径仍然写得进去**。把它并进"强度"一栏就会让人以为受保护了 ——
 * 那正是本项目此前最大的误导源。
 */
function sandboxWorkspaceBlock(j: SettingsResponse): string {
  const on = j.sandbox_env_redirected === true
  const ws = String(j.sandbox_workspace || '')
  if (!on) {
    return (
      '<div class="muted" style="margin-top:12px;padding:10px 12px;border:1px solid var(--line);border-radius:8px">' +
      '子进程落点：<b>未收窄</b>（随宿主工作目录）。声明档位（mode ≠ none）后会把外部工具子进程挪到会话私有目录。' +
      '</div>'
    )
  }
  return (
    '<div style="margin-top:12px;padding:10px 12px;border:1px solid var(--line);border-radius:8px">' +
    '<div style="font-weight:600">子进程落点：已收窄到会话私有目录</div>' +
    '<code style="display:block;margin-top:6px;word-break:break-all">' +
    esc(ws) +
    '</code>' +
    '<div class="muted" style="margin-top:8px">' +
    '外部工具子进程的 <code>cwd</code> 与缓存/临时目录指向此处。<b>这不是隔离强度</b>：' +
    '它只收窄相对路径的写入落点，<b>绝对路径仍然写得进去</b> —— 强度看上方的徽标。' +
    '</div></div>'
  )
}

/** 准入名单四键的中文标签（归属已改 guard：sys/guard.py 是策略唯一持有者） */
export const GUARD_FIELD_LABELS: Record<string, string> = {
  targets: '管辖范围 targets（工具名模式，逗号分隔）',
  default: '兜底策略 default（open=不先限制 / closed=白名单制）',
  allow: '放行名单 allow（工具名模式，逗号分隔）',
  deny: '拒绝名单 deny（工具名模式，逗号分隔）',
}

/** 准入兜底取值（与后端 `_SETTING_META` 里 `default` 键的 choice 集合一致） */
export const GUARD_DECISIONS = ['open', 'closed'] as const
export const GUARD_DECISION_LABELS: Record<string, string> = {
  open: '不先限制能力（未命中名单一律放行）',
  closed: '白名单制（未列入 allow 即拒）',
}

/** 保存时收集的数值键顺序（legacy app.js:552，原样） */
export const SETTINGS_NUMERIC_KEYS = [
  'max_tokens_per_run',
  'finalize_reserve_tokens',
  'compact_trigger_tokens',
  'max_context_tokens',
  'reserve_output_tokens',
  'memory_ratio',
  'history_ratio',
  'source_clip_tokens',
  'clip_max_tokens',
  'clip_head_tokens',
  'clip_tail_tokens',
  'react_keep_recent',
  'react_min_chain_tokens',
  'working_window_tokens',
  'window_high_ratio',
  'window_low_ratio',
  'resident_cap_tokens',
  'on_demand_cap_tokens',
  'prompt_cap_tokens',
  'clear_at_least_tokens',
  // ---- 执行闸门（2026-10-10）----
  'max_steps',
  'deadline_s',
] as const

/** 输入框通用内联样式（legacy 多处重复，原样保留字符串以逐字符对拍） */
const INP = 'width:240px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit'
const SEL = 'width:340px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit;background:#fff'
const HINT = 'font-size:11px;margin-top:3px'
const LABEL = 'display:block;margin:12px 0 0'
/** G 放行域下拉 · 触发器：外观与其它 select 一致（同宽 340px），右侧带展开指示 */
const DD_BTN =
  'display:flex;align-items:center;justify-content:space-between;gap:8px;width:340px;padding:7px 9px;' +
  'border:1px solid var(--line);border-radius:8px;font:inherit;background:#fff;color:inherit;cursor:pointer;text-align:left'
/** G 放行域下拉 · 浮层面板：默认 hidden 收起，点触发器才展开 */
const DD_PANEL =
  'position:absolute;z-index:30;margin-top:4px;width:340px;max-height:220px;overflow:auto;' +
  'border:1px solid var(--line);border-radius:8px;background:#fff;box-shadow:0 10px 28px rgba(15,23,42,.12);padding:6px'

/** legacy app.js:469-549 — 设置页 HTML（纯函数，逐字符对拍）
 *
 * G2（会话级化）扩展：新增 ``scope`` 参数（默认 ``container``，保持旧行为），
 * 菜单顶部给出「容器级 / 本会话」切换；会话作用域下：
 *  - 值显示为**生效值**（容器 ∪ 本会话）；
 *  - 被本会话显式覆盖的键加「本会话」徽标；
 *  - **仅容器级**的键（``j.scopable`` 之外）置灰禁用，避免误提交被后端 400。
 */
export function renderSettingsHtml(j: SettingsResponse, scope: SettingsScope = 'container'): string {
  const isSession = scope === 'session'
  // 容器作用域看「容器级生效值」，会话作用域看「容器 ∪ 本会话的生效值」
  const containerVals = (j.container || j.values || {}) as Record<string, unknown>
  const effVals = (j.effective || j.values || {}) as Record<string, unknown>
  const v = isSession ? effVals : containerVals
  const scopable = new Set(((j.scopable || []) as string[]).map(String))
  const sessionKeys = new Set(Object.keys((j.session || {}) as Record<string, unknown>))
  /** 本会话是否显式覆盖了该键（会话作用域下才显示，避免误导） */
  const sessTag = (k: string): string =>
    isSession && sessionKeys.has(k)
      ? ' <span class="tag blue" style="font-size:10px">本会话</span>'
      : ''
  /** 会话作用域下，容器级键禁用（提交也不会被接受） */
  const dis = (k: string): string => (isSession && !scopable.has(k) ? ' disabled' : '')
  const onlyContainer = (k: string): boolean => isSession && !scopable.has(k)
  // G 域级裁剪：白名单以「可勾选」呈现，候选项由后端给（= 已注册域 ∪ 已选但当前未注册域）。
  // 不手填 —— 域名是 owner（插件 id），手填既要记拼写、又会因拼错触发后端安全阀静默回退。
  const domainOpts = (j.domains || []) as SettingsDomainOption[]
  const pickedDomains = new Set(((v.tool_domains || []) as string[]).map((s) => String(s).trim()))
  const domainRows = domainOpts.length
    ? domainOpts
        .map(
          (d) =>
            '<label style="display:flex;align-items:center;gap:8px;padding:4px 0;font-weight:400;cursor:pointer">' +
            '<input type="checkbox" class="set_tool_domain_cb" value="' +
            esc(d.name) +
            '"' +
            (pickedDomains.has(d.name) ? ' checked' : '') +
            '>' +
            '<span style="font-family:var(--font-mono);font-size:12px">' +
            esc(d.name) +
            '</span>' +
            '<span class="muted" style="font-size:11px">' +
            (d.available === false ? '当前未注册' : esc(d.count) + ' 个工具') +
            '</span></label>',
        )
        .join('')
    : '<div class="muted" style="font-size:12px">（当前没有已注册的工具域）</div>'
  // 下拉触发条文案：≤2 个直接列名，更多折成计数；一个都没选 ⇒ 明示「不裁剪」
  const pickedNames = domainOpts.filter((d) => pickedDomains.has(d.name)).map((d) => d.name)
  const ddLabel = pickedNames.length
    ? pickedNames.length <= 2
      ? pickedNames.join('、')
      : '已选 ' + pickedNames.length + ' 个域'
    : '未选择（不裁剪）'
  let h =
    '<div class="card"><h3 style="margin-top:0">作用域（scope）</h3>' +
    '<div class="muted" style="margin-bottom:10px">' +
    '参数分两层：<b>容器级</b>（全局，保存后重建容器生效）与<b>本会话级</b>（只影响当前会话，保存后立即生效、<b>不重建容器</b>）。' +
    '会话级覆盖逐键优先于容器级，未覆盖的键沿用容器值（继承而非替换）。</div>' +
    '<label style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px">写入作用域 scope</div>' +
    '<select id="set_scope" style="' +
    SEL +
    '">' +
    '<option value="container"' +
    (isSession ? '' : ' selected') +
    '>容器级 container（全局；保存后重建容器生效）</option>' +
    '<option value="session"' +
    (isSession ? ' selected' : '') +
    '>本会话 session（当前：' +
    esc(j.session_id || 'default') +
    '；立即生效，不重建容器）</option>' +
    '</select>' +
    '<div class="muted" style="' +
    HINT +
    '">会话 = 请求头 X-Session-Id / 地址栏 ?sid=（网页「新会话」会切换）；会话级覆盖文件里按会话留存，'
    + '超上限时按最久未用淘汰（只删覆盖，不动转写/记忆/快照）。</div></label>' +
    (isSession
      ? '<div class="muted" style="font-size:11px">当前为<b>本会话</b>作用域：带「本会话」徽标的项已被覆盖，其余继承容器值；' +
        '置灰项<b>仅支持容器级</b>（如审批通道、懒加载/域裁剪等装配开关）。</div>'
      : '<div class="muted" style="font-size:11px">当前为<b>容器级</b>作用域：修改会影响所有会话；' +
        '只想调本会话，切到「本会话」。</div>') +
    '<div style="margin-top:10px"><button class="btn ghost" id="resetSessionSettings">重置为继承（清除本会话覆盖）</button></div>' +
    '</div>' +
    '<div class="card" style="margin-top:16px"><h3 style="margin-top:0">成本护栏（budget）</h3>' +
    '<div class="muted" style="margin-bottom:10px">LLM 调用的 token 上限：每次调用前先按估算预借、结束后按服务商真实用量对账；超限即拒绝调用。容器级保存后立即重建容器生效（<b>已用量重计</b>）；'
    + '会话级保存后下个请求即生效（<b>不重建、不清账</b>）。覆盖写入 data/agent_plugin_config.json，重启后保持。</div>' +
    '<label style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px">运行预算上限 max_tokens_per_run (token)' +
    sessTag('max_tokens_per_run') +
    '</div>' +
    '<input id="set_max_tokens_per_run" type="number" step="1000" min="1000" value="' +
    esc(v.max_tokens_per_run) +
    '" style="' +
    INP +
    '"' +
    dis('max_tokens_per_run') +
    '>' +
    '<div class="muted" style="' +
    HINT +
    '">当前默认 200000；本会话预算独立累计，网页「新会话」只清零<b>本会话</b>已用量</div></label>' +
    '<label style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px">收尾预留额度 finalize_reserve_tokens (token)' +
    sessTag('finalize_reserve_tokens') +
    '</div>' +
    '<input id="set_finalize_reserve_tokens" type="number" step="1000" min="0" value="' +
    esc(v.finalize_reserve_tokens) +
    '" style="' +
    INP +
    '"' +
    dis('finalize_reserve_tokens') +
    '>' +
    '<div class="muted" style="' +
    HINT +
    '">预算末段的收尾专用额度：正常调用只能用到「上限 − 预留」，触顶后的总结陈词可动用预留段，保证耗尽时也有体面收尾（0 = 关闭预留）。须不小于收尾窗口体积（建议 ≥ 24000）</div></label>' +
    '<label style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px"><input id="set_cost_calibration" type="checkbox"' +
    (v.cost_calibration ? ' checked' : '') +
    dis('cost_calibration') +
    '> 口径自校准 cost_calibration' +
    sessTag('cost_calibration') +
    '</div>' +
    (onlyContainer('cost_calibration')
      ? '<div class="muted" style="font-size:11px">仅容器级：校正是**模型属性**，按会话拆会让每个会话样本不足而退化成裸估算，精度反而更差。</div>'
      : '<div class="muted" style="font-size:11px">用服务商真实用量持续修正字符估算的偏差（实测约 30%），使预算判定与窗口装配贴近真实计费；样本不足 5 次时自动退回裸估算。关闭 = 回到纯估算口径。</div>') +
    '</label></div>' +
    '<div class="card" style="margin-top:16px"><h3 style="margin-top:0">循环内上下文治理（plugins.react）</h3>' +
    '<div class="muted" style="margin-bottom:10px">开启后每次请求只携带「工具目录卡」（名称+一行描述+参数名，无完整 JSON Schema），模型经 tool_search / tool_load 元工具按需加载完整定义，可显著降低工具定义的 token 开销。默认关闭（灰度）。</div>' +
    '<label style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px"><input id="set_lazy_tools" type="checkbox"' +
    (v.lazy_tools ? ' checked' : '') +
    dis('lazy_tools') +
    '> 启用懒加载 lazy_tools' +
    sessTag('lazy_tools') +
    '</div>' +
    '<div class="muted" style="font-size:11px">模型对未加载工具的调用会得到「未加载」错误观察并自行纠正；执行面闸门（审批/只读裁剪）不受影响。</div></label>' +
    '<label style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px"><input id="set_alignment_enabled" type="checkbox"' +
    (v.alignment_enabled ? ' checked' : '') +
    dis('alignment_enabled') +
    '> 续接前缀对齐 alignment_enabled' +
    sessTag('alignment_enabled') +
    '</div>' +
    '<div class="muted" style="font-size:11px">续接时冻结头部快照（system/前置上下文/首轮提示词）跨 run 逐字节稳定，新消息追加链尾，使请求构成前序请求的严格前缀，命中提示缓存（实测前缀断裂时整链按 miss 计价）。关闭 = 回退旧行为，续接时头部随当前提示词重建（并发出 prefix-unstable 事件）。</div></label>' +
    '<label style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px"><input id="set_tool_domains_enabled" type="checkbox"' +
    (v.tool_domains_enabled ? ' checked' : '') +
    dis('tool_domains_enabled') +
    '> 启用域级裁剪 tool_domains_enabled' +
    sessTag('tool_domains_enabled') +
    '</div>' +
    '<div class="muted" style="font-size:11px">按工具「域」(注册来源 owner = 插件 id) 裁剪暴露面：未列名的域对模型不可见，但仍可经 tool_search / tool_load 检索加载（<b>隐藏但可检索</b>）。<b>仅在懒加载开启时生效</b> —— 非懒加载没有检索通道，裁剪会让被隐藏工具彻底失联。默认关闭。</div></label>' +
    '<div style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px">放行域白名单 tool_domains' +
    sessTag('tool_domains') +
    '</div>' +
    '<div style="position:relative;width:340px">' +
    '<button type="button" id="set_tool_domains_btn" aria-haspopup="true" aria-expanded="false"' +
    dis('tool_domains') +
    ' style="' +
    DD_BTN +
    '"><span id="set_tool_domains_label" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
    esc(ddLabel) +
    '</span><span class="muted" style="flex:none;font-size:10px">▾</span></button>' +
    '<div id="set_tool_domains" class="set_tool_domain_panel" hidden style="' +
    DD_PANEL +
    '">' +
    domainRows +
    '</div></div>' +
    '<div class="muted" style="' +
    HINT +
    '">放行制：勾选的域对模型可见，其余域被隐藏（仍可经 tool_search 检索加载）；<b>一个都不勾 = 不裁剪</b>（不做「全隐藏」）。域 = 工具注册来源 owner（插件 id），新增 MCP 插件会自动成为一个新域，在此自动出现。</div></div></div>' +
    // ---- 执行闸门（2026-10-10 基线驱动）----
    // 独立卡片而非并入上面的「上下文治理」：这两个是**循环的执行闸**（步数/时限），
    // 与「装配什么上下文」是不同关注点，混在一张卡里会让用户为调步数而去动上下文参数。
    '<div class="card" style="margin-top:16px"><h3 style="margin-top:0">执行闸门（plugins.react）</h3>' +
    '<div class="muted" style="margin-bottom:10px">多步循环的<b>停止条件</b>：步数用尽或时限用尽即触发「诚实收尾」（基于已有信息作答，不静默截断）。' +
    '实测基线（298 次运行）显示 <b>步数上限是最大触顶原因（占 44%）</b>，故一并暴露；无进展检测（防原地打转）已默认启用，无需设置。容器级保存后立即重建容器生效。</div>' +
    '<label style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px">最大步数 max_steps' +
    sessTag('max_steps') +
    '</div>' +
    '<input id="set_max_steps" type="number" step="1" min="1" max="200" value="' +
    esc(v.max_steps) +
    '" style="' +
    INP +
    '"' +
    dis('max_steps') +
    '>' +
    '<div class="muted" style="' +
    HINT +
    '">一次任务最多几轮「推理 + 工具调用」。<b>默认 8</b>；复杂任务若频繁触顶（表现为「已达步数上限」），可调到 16~25。' +
    '调大前请确认：模型若在原地打转，加步数只是让它转得更久——无进展检测会兜底，但代价是 token。</div></label>' +
    '<label style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px">单次运行时限 deadline_s (秒)' +
    sessTag('deadline_s') +
    '</div>' +
    '<input id="set_deadline_s" type="number" step="5" min="5" max="3600" value="' +
    esc(v.deadline_s) +
    '" style="' +
    INP +
    '"' +
    dis('deadline_s') +
    '>' +
    '<div class="muted" style="' +
    HINT +
    '">一次任务的墙钟时限（秒），<b>默认 120</b>。与步数闸<b>成对</b>生效：先到者触发收尾。' +
    '工具调用是串行执行的，轮数多时可能先撞时限——此时应同时看 max_steps。</div></label></div>'

  if (j.context_plugin_enabled !== true) {
    h +=
      '<div class="card muted" style="margin-top:16px">上下文管理插件（plugins.context）未启用 —— 在「插件」页点「启用」后，可在此调整参数。</div>'
  } else {
    h +=
      '<h3 style="margin-top:16px">上下文管理 <span class="tag blue">plugins.context 已启用</span></h3>' +
      '<div class="card"><div class="muted" style="margin-bottom:10px">启用上下文管理插件时可调；保存后立即重建容器生效。覆盖写入 data/agent_plugin_config.json，重启后保持。</div>'
    h += CONTEXT_FIELDS.map(
      ([k, label, hint]) =>
        '<label style="' +
        LABEL +
        '"><div style="font-weight:600;margin-bottom:3px">' +
        esc(label) +
        sessTag(k) +
        '</div>' +
        '<input id="set_' +
        k +
        '" type="number" step="' +
        (isFloatKey(k) ? '0.01' : '1') +
        '" value="' +
        esc(v[k]) +
        '" style="' +
        INP +
        '"' +
        dis(k) +
        '>' +
        '<div class="muted" style="' +
        HINT +
        '">' +
        esc(hint) +
        '</div></label>',
    ).join('')
    h +=
      '<label style="' +
      LABEL +
      '"><div style="font-weight:600;margin-bottom:3px">压缩模式 compact_mode' +
      sessTag('compact_mode') +
      '</div>' +
      '<select id="set_compact_mode" style="' +
      SEL +
      '"' +
      dis('compact_mode') +
      '>' +
      COMPACT_MODES.map(
        (o) =>
          '<option value="' + o + '"' + (v.compact_mode === o ? ' selected' : '') + '>' + o + '（' + COMPACT_MODE_LABELS[o] + '）</option>',
      ).join('') +
      '</select><div class="muted" style="' +
      HINT +
      '">治理动词（clear/compact）的执行开关；默认 shadow（只观测不改变装配行为）。链压缩仅在 run 收尾存档点、且过回本闸时发生。</div></label>'
    h += '</div>'
  }

  if (j.approval_enabled === false) {
    h +=
      '<div class="card muted" style="margin-top:16px">审批插件（plugins.approval）未启用 —— 在「插件」页点「启用」后，可在此调整。</div>'
  } else {
    h +=
      '<div class="card" style="margin-top:16px"><h3 style="margin-top:0">审批 · 人在循环（plugins.approval）</h3>' +
      '<div class="muted" style="margin-bottom:10px">调整审批通道与名单；保存后立即重建容器生效（会话档位回到默认）。覆盖写入 data/agent_plugin_config.json，重启后保持。</div>' +
      '<label style="' +
      LABEL +
      '"><div style="font-weight:600;margin-bottom:3px">问人通道 provider' +
      sessTag('provider') +
      '</div>' +
      '<select id="set_provider" style="' +
      SEL +
      '"' +
      dis('provider') +
      '>' +
      APPROVAL_PROVIDERS.map(
        (o) =>
          '<option value="' +
          o +
          '"' +
          (v.provider === o ? ' selected' : '') +
          '>' +
          o +
          '（' +
          APPROVAL_PROVIDER_LABELS[o] +
          '）</option>',
      ).join('') +
      '</select><div class="muted" style="' +
      HINT +
      '">仅 web / cli 为交互式，会真正等人答复（web 才有网页弹窗）</div></label>' +
      '<label style="' +
      LABEL +
      '"><div style="font-weight:600;margin-bottom:3px">等待时限 timeout_s（秒）' +
      sessTag('timeout_s') +
      '</div>' +
      '<input id="set_timeout_s" type="number" step="1" min="1" value="' +
      esc(v.timeout_s) +
      '" style="' +
      INP +
      '"' +
      dis('timeout_s') +
      '>' +
      '<div class="muted" style="' +
      HINT +
      '">无人应答超过此时限 → 默认拒绝（fail-safe）</div></label>' +
      '<label style="' +
      LABEL +
      '"><div style="font-weight:600;margin-bottom:3px">必问名单 ask（fnmatch，逗号分隔）' +
      sessTag('ask') +
      '</div>' +
      '<input id="set_ask" type="text" value="' +
      esc(((v.ask || []) as string[]).join(', ')) +
      '" placeholder="如 t.write*, *delete*" style="width:360px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit"' +
      dis('ask') +
      '>' +
      '<div class="muted" style="' +
      HINT +
      '">命中名单的工具执行前必问人；配合对话页「审批」档可让所有非只读工具都问</div></label></div>'
  }

  // ---- 准入名单（sys.guard：策略唯一持有者）----
  // 2026-03 收口：四键从 plugins.sandbox 改挂 guard（它是 LLM_PRE_CALL 的唯一挂载者，
  // 也是唯一覆盖非 MCP 内置工具的裁决者）。经 `run/chat.py::_SECTION_ALIAS` 落到
  // `agent.permissions`（guard 的矩阵 SSOT 节）。
  {
    const listInp = (k: string, ph: string, hint: string): string =>
      '<label style="' +
      LABEL +
      '"><div style="font-weight:600;margin-bottom:3px">' +
      esc(GUARD_FIELD_LABELS[k]) +
      sessTag(k) +
      '</div>' +
      '<input id="set_' +
      k +
      '" type="text" value="' +
      esc(((v[k] || []) as string[]).join(', ')) +
      '" placeholder="' +
      esc(ph) +
      '" style="width:360px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit"' +
      dis(k) +
      '>' +
      '<div class="muted" style="' +
      HINT +
      '">' +
      esc(hint) +
      '</div></label>'
    h +=
      '<div class="card" style="margin-top:16px"><h3 style="margin-top:0">准入名单（sys.guard）</h3>' +
      '<div class="muted" style="margin-bottom:10px">决定<b>谁能调什么</b>。白名单模式（<code>default=closed</code>）下未列入 allow 的调用一律拒；黑名单优先，白名单不能穿透。每次裁决落审计账本（见「权限」视图的「最近裁决」）。<b>启用 ≠ 已收紧</b>：默认 <code>open</code>，本层不先限制能力。</div>' +
      // ⚠️ 高危开关告知：llm 闸不在设置页可写（见 _SETTING_META），但运维可能手改配置文件，
      // 所以必须在界面上讲清后果 + 收紧后的行为（2026-10-03：取值域收为 open/deny，
      // 误配回落 open 并计入 agent_guard_llm_mode_invalid_total）。
      '<div style="margin-bottom:10px;padding:8px 10px;border:1px solid var(--line);border-radius:8px">' +
      '<b>⚠️ 高危开关：<code>permissions.llm</code>（本设置页<b>不可写</b>，仅配置文件）</b>' +
      '<div class="muted" style="margin-top:4px">配成 <code>deny</code> 会拒掉<b>全部</b>模型调用 —— ' +
      '记忆提取 / 结果解说 / 反思都在内，等于让服务失去大部分能力。合法取值只有 <code>open</code>（默认）与 <code>deny</code>；' +
      '其他任何值（<code>true</code> / <code>1</code> / 乱写）会被视为 <code>open</code> 放行并计入告警计数，' +
      '不会再"误配即锁死"。</div></div>' +
      listInp('targets', '*, mcp.*, plugins.*', '受管辖的工具名模式（逗号分隔）。尾部 * 通配；不在此列表的调用完全不过问。<b>默认 * = 全部</b>。') +
      '<label style="' +
      LABEL +
      '"><div style="font-weight:600;margin-bottom:3px">兜底策略 default' +
      sessTag('default') +
      '</div>' +
      '<select id="set_default" style="' +
      SEL +
      '"' +
      dis('default') +
      '>' +
      GUARD_DECISIONS.map(
        (o) =>
          '<option value="' +
          o +
          '"' +
          (v.default === o ? ' selected' : '') +
          '>' +
          o +
          '（' +
          GUARD_DECISION_LABELS[o] +
          '）</option>',
      ).join('') +
      '</select><div class="muted" style="' +
      HINT +
      '"><b>open</b> = 未命中名单一律放行（默认，不先限制能力）；<b>closed</b> = 白名单制，未列入 allow 即拒。</div></label>' +
      listInp('allow', 'agent.ping, mcp.playwright.*', '白名单模式（<code>default=closed</code> 时才生效）。<b>不能穿透黑名单</b>（deny 优先判定）。') +
      listInp('deny', 'mcp.windows.*', '黑名单模式；命中即拒，审计与报错都会写明命中的是哪一条。') +
      '</div>'
  }

  // ---- 隔离档位（plugins.sandbox：confinement 提供方）----
  // 目录插件默认关 ⇒ 未启用时明确提示"不受本层管辖"，避免"以为受保护"的错觉。
  if (j.sandbox_enabled === false) {
    h +=
      '<div class="card muted" style="margin-top:16px">外部工具隔离档位（plugins.sandbox）未启用 —— 在「插件」页点「启用」后，可在此调整。<b>启用 ≠ 有隔离</b>：当前后端无任何进程级隔离实现，启用只增加可追溯性。</div>'
  } else {
    const enf = (j.sandbox_enforcement || 'none') as string
    const badge = ENFORCEMENT_BADGES[enf] || ENFORCEMENT_BADGES.none
    h +=
      '<div class="card" style="margin-top:16px"><h3 style="margin-top:0">外部工具隔离 · 沙箱（plugins.sandbox）</h3>' +
      '<div class="muted" style="margin-bottom:10px">回答<b>「这次调用在什么隔离档位下跑、实际强度多少」</b>，<b>不判准入</b>（准入见上一张卡）。每次调用落 <code>agent/sandbox/confinement</code> 审计（见「事件」视图的「沙箱」过滤项）。</div>' +
      '<div style="margin-bottom:12px;padding:10px 12px;border:1px solid var(--line);border-radius:8px">' +
      '<span style="font-weight:600">当前实际强度：</span>' +
      '<span title="' +
      esc(badge.title) +
      '" style="margin-left:8px;padding:2px 8px;border-radius:999px;background:' +
      badge.color +
      ';color:#fff;font-weight:600">' +
      esc(badge.text) +
      '</span>' +
      '<div class="muted" style="margin-top:8px">' +
      esc(badge.title) +
      '</div></div>' +
      '<label style="' +
      LABEL +
      '"><div style="font-weight:600;margin-bottom:3px">声明档位 mode' +
      sessTag('mode') +
      '</div>' +
      '<select id="set_mode" style="' +
      SEL +
      '"' +
      dis('mode') +
      '>' +
      SANDBOX_MODES.map(
        (o) =>
          '<option value="' +
          o +
          '"' +
          (v.mode === o ? ' selected' : '') +
          '>' +
          o +
          '（' +
          SANDBOX_MODE_LABELS[o] +
          '）</option>',
      ).join('') +
      '</select><div class="muted" style="' +
      HINT +
      '">声明<b>想</b>限制到什么。<b>当前后端无进程级隔离实现 ⇒ 声明了也兑现不了</b>（审计里会记 <code>gap=true</code>）。这比"看起来受保护"诚实。</div></label>' +
      '<label style="' +
      LABEL +
      '"><div style="font-weight:600;margin-bottom:3px">自述范围 targets' +
      sessTag('targets') +
      '</div>' +
      '<input id="set_sb_targets" type="text" value="' +
      esc(((j.sandbox_targets || ['mcp.*']) as string[]).join(', ')) +
      '" disabled style="width:360px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit;opacity:.6">' +
      '<div class="muted" style="' +
      HINT +
      '">本层只自述 <code>mcp.*</code> 域（隔离是<b>按桥</b>配置的：每个 MCP 桥一个子进程）。改管辖范围请用上一张卡的 <code>targets</code>。</div></label>' +
      sandboxWorkspaceBlock(j) +
      '</div>'
  }

  h +=
    '<div style="margin-top:16px"><button class="btn" id="saveSettings">' +
    (isSession ? '保存到本会话' : '保存（容器级）') +
    '</button> ' +
    '<button class="btn ghost" id="resetSettings">恢复默认（容器级）</button> ' +
    '<span class="muted" id="setMsg"></span></div>'
  return h
}
