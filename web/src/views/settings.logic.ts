/**
 * SettingsView 视图逻辑（B2.3 迁移自 legacy app.js:469-575）
 * ------------------------------------------------------------------
 * 拆为「字段描述表（纯数据）」+「HTML 生成（纯函数）」+「补丁收集（纯函数）」，
 * 三者均可独立对拍；交互由 Vue 组件驱动。
 * 时间：2026-09-30 11:40 (GMT+8)
 */

import { esc } from '../components/markdown'
import type { SettingsResponse, SettingsDomainOption } from '../api/types'

/** 浮点型字段（legacy app.js:514、551 两处硬编码，此处收敛为一处） */
export const FLOAT_KEYS = new Set([
  'memory_ratio',
  'history_ratio',
  'window_high_ratio',
  'window_low_ratio',
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

/** legacy app.js:469-549 — 设置页 HTML（纯函数，逐字符对拍） */
export function renderSettingsHtml(j: SettingsResponse): string {
  const v = (j.values || {}) as Record<string, unknown>
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
    '<div class="card"><h3 style="margin-top:0">成本护栏（budget）</h3>' +
    '<div class="muted" style="margin-bottom:10px">LLM 调用的生命周期累计 token 上限：每次调用前先按估算预借、结束后按服务商真实用量对账；超限即拒绝调用。保存后立即重建容器生效（<b>已用量重计</b>）。覆盖写入 data/agent_plugin_config.json，重启后保持。</div>' +
    '<label style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px">运行预算上限 max_tokens_per_run (token)</div>' +
    '<input id="set_max_tokens_per_run" type="number" step="1000" min="1000" value="' +
    esc(v.max_tokens_per_run) +
    '" style="' +
    INP +
    '">' +
    '<div class="muted" style="' +
    HINT +
    '">当前默认 200000；网页「新会话」也会把已用量归零重计</div></label>' +
    '<label style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px">收尾预留额度 finalize_reserve_tokens (token)</div>' +
    '<input id="set_finalize_reserve_tokens" type="number" step="1000" min="0" value="' +
    esc(v.finalize_reserve_tokens) +
    '" style="' +
    INP +
    '">' +
    '<div class="muted" style="' +
    HINT +
    '">预算末段的收尾专用额度：正常调用只能用到「上限 − 预留」，触顶后的总结陈词可动用预留段，保证耗尽时也有体面收尾（0 = 关闭预留）。须不小于收尾窗口体积（建议 ≥ 24000）</div></label>' +
    '<label style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px"><input id="set_cost_calibration" type="checkbox"' +
    (v.cost_calibration ? ' checked' : '') +
    '> 口径自校准 cost_calibration</div>' +
    '<div class="muted" style="font-size:11px">用服务商真实用量持续修正字符估算的偏差（实测约 30%），使预算判定与窗口装配贴近真实计费；样本不足 5 次时自动退回裸估算。关闭 = 回到纯估算口径。</div></label></div>' +
    '<div class="card" style="margin-top:16px"><h3 style="margin-top:0">循环内上下文治理（plugins.react）</h3>' +
    '<div class="muted" style="margin-bottom:10px">开启后每次请求只携带「工具目录卡」（名称+一行描述+参数名，无完整 JSON Schema），模型经 tool_search / tool_load 元工具按需加载完整定义，可显著降低工具定义的 token 开销。默认关闭（灰度）。</div>' +
    '<label style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px"><input id="set_lazy_tools" type="checkbox"' +
    (v.lazy_tools ? ' checked' : '') +
    '> 启用懒加载 lazy_tools</div>' +
    '<div class="muted" style="font-size:11px">模型对未加载工具的调用会得到「未加载」错误观察并自行纠正；执行面闸门（审批/只读裁剪）不受影响。</div></label>' +
    '<label style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px"><input id="set_alignment_enabled" type="checkbox"' +
    (v.alignment_enabled ? ' checked' : '') +
    '> 续接前缀对齐 alignment_enabled</div>' +
    '<div class="muted" style="font-size:11px">续接时冻结头部快照（system/前置上下文/首轮提示词）跨 run 逐字节稳定，新消息追加链尾，使请求构成前序请求的严格前缀，命中提示缓存（实测前缀断裂时整链按 miss 计价）。关闭 = 回退旧行为，续接时头部随当前提示词重建（并发出 prefix-unstable 事件）。</div></label>' +
    '<label style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px"><input id="set_tool_domains_enabled" type="checkbox"' +
    (v.tool_domains_enabled ? ' checked' : '') +
    '> 启用域级裁剪 tool_domains_enabled</div>' +
    '<div class="muted" style="font-size:11px">按工具「域」(注册来源 owner = 插件 id) 裁剪暴露面：未列名的域对模型不可见，但仍可经 tool_search / tool_load 检索加载（<b>隐藏但可检索</b>）。<b>仅在懒加载开启时生效</b> —— 非懒加载没有检索通道，裁剪会让被隐藏工具彻底失联。默认关闭。</div></label>' +
    '<div style="' +
    LABEL +
    '"><div style="font-weight:600;margin-bottom:3px">放行域白名单 tool_domains</div>' +
    '<div style="position:relative;width:340px">' +
    '<button type="button" id="set_tool_domains_btn" aria-haspopup="true" aria-expanded="false" style="' +
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
    '">放行制：勾选的域对模型可见，其余域被隐藏（仍可经 tool_search 检索加载）；<b>一个都不勾 = 不裁剪</b>（不做「全隐藏」）。域 = 工具注册来源 owner（插件 id），新增 MCP 插件会自动成为一个新域，在此自动出现。</div></div></div>'

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
        '</div>' +
        '<input id="set_' +
        k +
        '" type="number" step="' +
        (isFloatKey(k) ? '0.01' : '1') +
        '" value="' +
        esc(v[k]) +
        '" style="' +
        INP +
        '">' +
        '<div class="muted" style="' +
        HINT +
        '">' +
        esc(hint) +
        '</div></label>',
    ).join('')
    h +=
      '<label style="' +
      LABEL +
      '"><div style="font-weight:600;margin-bottom:3px">压缩模式 compact_mode</div>' +
      '<select id="set_compact_mode" style="' +
      SEL +
      '">' +
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
      '"><div style="font-weight:600;margin-bottom:3px">问人通道 provider</div>' +
      '<select id="set_provider" style="' +
      SEL +
      '">' +
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
      '"><div style="font-weight:600;margin-bottom:3px">等待时限 timeout_s（秒）</div>' +
      '<input id="set_timeout_s" type="number" step="1" min="1" value="' +
      esc(v.timeout_s) +
      '" style="' +
      INP +
      '">' +
      '<div class="muted" style="' +
      HINT +
      '">无人应答超过此时限 → 默认拒绝（fail-safe）</div></label>' +
      '<label style="' +
      LABEL +
      '"><div style="font-weight:600;margin-bottom:3px">必问名单 ask（fnmatch，逗号分隔）</div>' +
      '<input id="set_ask" type="text" value="' +
      esc(((v.ask || []) as string[]).join(', ')) +
      '" placeholder="如 t.write*, *delete*" style="width:360px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit">' +
      '<div class="muted" style="' +
      HINT +
      '">命中名单的工具执行前必问人；配合对话页「审批」档可让所有非只读工具都问</div></label></div>'
  }

  h +=
    '<div style="margin-top:16px"><button class="btn" id="saveSettings">保存</button> ' +
    '<button class="btn ghost" id="resetSettings">恢复默认</button> <span class="muted" id="setMsg"></span></div>'
  return h
}
