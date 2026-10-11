<template>
  <div class="panel">
    <!-- 新增技能表单 -->
    <div class="card" style="margin-bottom:16px">
      <h3 style="margin-top:0">新增 / 编辑技能（写入 data/skills/*.md）</h3>
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-start">
        <div style="flex:1;min-width:200px">
          <label style="display:block;margin:8px 0 0">名称（skill.xxx）</label>
          <input v-model="form.name" placeholder="如 skill.pdf_extract" style="width:100%" />
          <label style="display:block;margin:8px 0 0">摘要（一句话，展示在索引）</label>
          <input v-model="form.description" placeholder="一句话说明何时用、做什么" style="width:100%" />
          <label style="display:block;margin:8px 0 0">风险档 / 标签</label>
          <div style="display:flex;gap:8px">
            <select v-model="form.risk" style="flex:1">
              <option value="read">read（只读）</option>
              <option value="write">write（写类）</option>
            </select>
            <input v-model="form.tags" placeholder="标签逗号分隔" style="flex:2" />
          </div>
        </div>
        <div style="flex:2;min-width:300px">
          <label style="display:block;margin:8px 0 0">说明正文（markdown，模型加载后照着做）</label>
          <textarea v-model="form.content" rows="6" placeholder="遇到 X 类任务时，按以下步骤做：1... 2..." style="width:100%;font-family:var(--font-mono)"></textarea>
        </div>
      </div>
      <div style="margin-top:10px;display:flex;gap:10px;align-items:center">
        <button class="btn ghost" @click="addOrUpdate">{{ editing ? '保存修改' : '新增技能' }}</button>
        <button class="btn ghost" @click="resetForm" v-if="editing">取消编辑</button>
        <span class="muted" style="font-size:11px">新增默认写盘持久化；提交后重启无需重新添加。</span>
      </div>
    </div>

    <!-- 技能列表（由 renderSkillsHtml 生成） -->
    <div ref="host" v-html="html" @click="onClick"></div>

    <!-- 查看弹窗 -->
    <div v-if="viewing" class="modal-mask" @click.self="viewing = false">
      <div class="modal">
        <div class="modal-head">
          <b>{{ viewing.name }}</b>
          <button class="chip" @click="viewing = false">关闭</button>
        </div>
        <div class="modal-body">
          <div class="muted" style="margin-bottom:6px">{{ viewing.description }}</div>
          <pre style="white-space:pre-wrap;font-family:var(--font-mono);font-size:12px">{{ viewing.content }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * SkillsView（镜像 PluginsView 的 B2.3 范式）
 * ------------------------------------------------------------------
 * 渲染委托给 `renderSkillsHtml`（纯函数）；本组件负责：数据加载、列表事件委托、
 * 新增/编辑/启停/移除/查看交互。技能系统未启用时给出提示。
 */
import { ref, onMounted, computed } from 'vue'
import { renderSkillsHtml } from './skills.logic'
import {
  skillsList,
  skillGet,
  skillAdd,
  skillUpdate,
  skillRemove,
} from '../api/endpoints'
import type { SkillRow } from '../api/types'

const rows = ref<SkillRow[]>([])
const enabled = ref(true)
const host = ref<HTMLElement | null>(null)
const busy = ref(false)

const editing = ref<string | null>(null)
const form = ref({ name: '', description: '', content: '', risk: 'read', tags: '' })

const viewing = ref<SkillRow | null>(null)

const html = computed(() => {
  if (!enabled.value) {
    return '<div class="muted" style="margin-top:8px">技能系统未启用（plugins.skills 未装配/未启用）。</div>'
  }
  return renderSkillsHtml(rows.value)
})

async function reload(): Promise<void> {
  try {
    const j = await skillsList()
    enabled.value = j.enabled !== false
    rows.value = j.skills || []
  } catch (e) {
    enabled.value = false
    alert('加载技能失败：' + (e as Error).message)
  }
}

function resetForm(): void {
  editing.value = null
  form.value = { name: '', description: '', content: '', risk: 'read', tags: '' }
}

async function addOrUpdate(): Promise<void> {
  if (busy.value) return
  const name = form.value.name.trim()
  if (!name) {
    alert('填技能名（建议 skill.xxx）')
    return
  }
  busy.value = true
  try {
    const body = {
      name,
      description: form.value.description.trim(),
      content: form.value.content,
      risk: form.value.risk,
      tags: form.value.tags
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
    }
    if (editing.value) {
      await skillUpdate(name, body)
    } else {
      await skillAdd(body)
    }
    resetForm()
    await reload()
  } catch (e) {
    alert('操作失败：' + (e as Error).message)
  } finally {
    busy.value = false
  }
}

function startEdit(s: SkillRow): void {
  editing.value = s.name
  form.value = {
    name: s.name,
    description: s.description || '',
    content: s.content || '',
    risk: s.risk || 'read',
    tags: (s.tags || []).join(', '),
  }
}

async function toggle(name: string, enable: string): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    await skillUpdate(name, { enabled: enable === 'true' })
    await reload()
  } catch (e) {
    alert('操作失败：' + (e as Error).message)
  } finally {
    busy.value = false
  }
}

async function remove(name: string): Promise<void> {
  if (!confirm('移除技能 ' + name + ' ？（文件型将删除 data/skills 下对应 .md）')) return
  if (busy.value) return
  busy.value = true
  try {
    await skillRemove(name, { persist: true })
    await reload()
  } catch (e) {
    alert('操作失败：' + (e as Error).message)
  } finally {
    busy.value = false
  }
}

async function view(name: string): Promise<void> {
  try {
    viewing.value = await skillGet(name)
  } catch (e) {
    alert('查看失败：' + (e as Error).message)
  }
}

function onClick(e: MouseEvent): void {
  const b = (e.target as HTMLElement).closest<HTMLElement>('[data-skill-act]')
  if (!b) return
  const act = b.dataset.skillAct || ''
  const name = b.dataset.name || ''
  if (act === 'view') view(name)
  else if (act === 'toggle') toggle(name, b.dataset.enable || 'true')
  else if (act === 'remove') remove(name)
  else if (act === 'edit') {
    const s = rows.value.find((r) => r.name === name)
    if (s) startEdit(s)
  }
}

onMounted(reload)

defineExpose({ reload })
</script>

<style scoped>
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
}
.modal {
  background: #fff;
  border-radius: 12px;
  width: min(720px, 92vw);
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 20px 60px rgba(15, 23, 42, 0.25);
}
.modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--line);
}
.modal-body {
  padding: 16px;
  overflow: auto;
}
</style>
