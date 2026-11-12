<script setup>
import { computed, nextTick, ref } from 'vue'
import api from '../api'
import { formatClock } from '../time'

const props = defineProps({ user: Object })
const emit = defineEmits(['changed', 'open-operation'])

const messages = ref([])
const question = ref('')
const conversationId = ref(null)
const loading = ref(false)
const creating = ref(false)
const actionBusy = ref(false)
const errorMsg = ref('')
const notice = ref('')
const scroller = ref(null)

const overviewOnly = computed(() => !(props.user?.permissions || []).includes('logs.view'))
const canCreate = computed(() => (props.user?.permissions || []).includes('operation.create'))
const canExecute = computed(() => (props.user?.permissions || []).includes('ops.execute'))
const examples = computed(() => [
  '0931 服务器为什么出现问题？',
  '0931 的磁盘使用率是多少？',
  '查看 0931 服务器最近的日志报错',
  '0931 的调用链哪里变慢了？',
  '0932 服务器现在正常吗？',
  ...(canExecute.value ? ['帮我清理内存'] : []),
])

function flash(text) {
  notice.value = text
  setTimeout(() => { notice.value = '' }, 4000)
}

function toolDetail(t) {
  const parts = [`工具 ${t.tool}`]
  for (const [k, v] of Object.entries(t || {})) {
    if (k === 'tool') continue
    const s = typeof v === 'string' ? v : JSON.stringify(v)
    parts.push(`${k} ${String(s).slice(0, 90)}`)
  }
  return parts.join(' · ')
}

function explainScope() {
  flash(overviewOnly.value
    ? '当前角色仅可查看指标概览，原始日志与链路数据按权限矩阵隐藏'
    : '当前角色可查看完整指标、原始日志与调用链数据')
}

async function scrollBottom() {
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
}

async function ask(preset) {
  const text = (typeof preset === 'string' ? preset : question.value).trim()
  if (!text || loading.value) return
  question.value = ''
  errorMsg.value = ''
  messages.value.push({ role: 'user', content: text, created_at: formatClock() })
  loading.value = true
  await scrollBottom()
  try {
    const data = await api.post('/chat/ask', {
      question: text,
      conversation_id: conversationId.value,
    })
    conversationId.value = data.conversation_id
    messages.value.push({
      role: 'assistant',
      content: data.answer,
      structured: data.structured,
      action_proposal: data.action_proposal || null,
      action_done: false,
      tool_trace: data.structured?.tool_trace || [],
      in_scope: data.in_scope,
      created_at: formatClock(),
    })
    emit('changed')
  } catch (e) {
    const detail = e.response?.data?.detail || '服务不可用'
    errorMsg.value = detail
    messages.value.push({ role: 'assistant', content: `请求失败：${detail}`, in_scope: false })
  } finally {
    loading.value = false
    await scrollBottom()
  }
}

async function decideAction(m, decision) {
  if (actionBusy.value || m.action_done || !m.action_proposal?.token) return
  actionBusy.value = true
  try {
    const d = await api.post('/chat/action', { token: m.action_proposal.token, decision })
    m.action_done = true
    messages.value.push({
      role: 'assistant',
      content: d.answer,
      structured: null,
      action_proposal: null,
      action_result: d.action_result,
      in_scope: true,
      created_at: formatClock(),
    })
    emit('changed')
    if (d.operation_code) flash(`处置已执行，工单 ${d.operation_code} 已同步到四端工作台`)
    await scrollBottom()
  } catch (e) {
    const detail = e.response?.data?.detail || '操作失败'
    m.action_done = true
    flash(detail)
  } finally { actionBusy.value = false }
}

async function createOperation(m) {
  if (!m.structured || creating.value) return
  creating.value = true
  try {
    const s = m.structured
    const problem = (s.problem || '排障结论').slice(0, 46)
    const d = await api.post('/operations', {
      server: s.server || '0931',
      title: `${s.server || '服务器'} 故障处理：${problem}`,
      issue: `问题：${s.problem}\n故障位置：${s.location}\n建议措施：${(s.suggestions || []).join('；')}`,
      source_conversation_id: conversationId.value,
    })
    emit('changed')
    flash(`已生成工单 ${d.record.code}，可在工作台查看四端联动记录`)
    emit('open-operation', d.record.id)
  } catch (e) {
    flash(e.response?.data?.detail || '发起处理失败')
  } finally { creating.value = false }
}

function newChat() {
  messages.value = []
  conversationId.value = null
  question.value = ''
  errorMsg.value = ''
  notice.value = ''
}

async function loadConversation(id) {
  try {
    const data = await api.get(`/chat/conversations/${id}`)
    conversationId.value = id
    messages.value = data.messages.map((m) => {
      const s = m.structured
      // 处置提案/结果是结构化消息，历史回放时还原成对应卡片，不当成排障结论
      if (s?.kind === 'action_proposal') {
        const { kind, token, ...proposal } = s
        return { role: m.role, content: m.content, structured: null,
                 action_proposal: proposal, action_done: true,
                 tool_trace: [], in_scope: true, created_at: m.created_at }
      }
      if (s?.kind === 'action_result') {
        return { role: m.role, content: m.content, structured: null,
                 action_result: s, tool_trace: [], in_scope: true,
                 created_at: m.created_at }
      }
      return {
        role: m.role, content: m.content, structured: s,
        tool_trace: m.tool_trace || [],
        in_scope: !!s,
        created_at: m.created_at,
      }
    })
    await scrollBottom()
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '加载对话失败'
  }
}

defineExpose({ newChat, loadConversation })
</script>

<template>
  <div class="chat">
    <div ref="scroller" class="stream">
      <div v-if="!messages.length" class="welcome">
        <div class="welcome-logo">
          <svg width="34" height="34" viewBox="0 0 24 24" fill="none">
            <path d="M4 15c3-7 8-9 16-8-1 8-5 12-11 11-2 0-4-1-5-3z" fill="#fff"/>
            <path d="M7 16c3-3 6-4 9-4" stroke="#4c6fff" stroke-width="1.6" stroke-linecap="round"/>
          </svg>
        </div>
        <h1>有什么服务器问题需要排查？</h1>
        <p class="muted">我会按问题调取指标、日志或链路数据，给出有依据的排障结论</p>
        <div class="examples">
          <button v-for="q in examples" :key="q" class="example" @click="ask(q)">{{ q }}</button>
        </div>
        <p v-if="overviewOnly" class="scope-tip">当前角色仅可查看概览数据，原始日志与链路不可见</p>
        <p class="domain-tip">仅支持服务器故障排障类专业问题，其他对话会被提示不支持</p>
      </div>

      <p v-if="notice" class="notice">{{ notice }}</p>

      <div v-for="(m, i) in messages" :key="i" class="msg-row" :class="m.role">
        <div v-if="m.role === 'user'" class="user-col">
          <div class="bubble-user">{{ m.content }}</div>
          <div class="msg-time muted">{{ m.created_at }}</div>
        </div>

        <div v-else class="assistant">
          <div class="assistant-avatar">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
              <path d="M4 15c3-7 8-9 16-8-1 8-5 12-11 11-2 0-4-1-5-3z" fill="#fff"/>
            </svg>
          </div>
          <div class="assistant-body">
            <!-- 直接处置命令：Agent 先询问，按钮都是真实交互 -->
            <div v-if="m.action_proposal" class="result-card action-card">
              <div class="result-head">
                <span class="badge">{{ m.action_proposal.allowed ? '处置确认' : '无法执行' }}</span>
                <span class="muted meta">{{ m.action_proposal.summary }}</span>
              </div>
              <div class="plain-answer" :class="{ 'out-of-scope': !m.action_proposal.allowed }">{{ m.content }}</div>
              <div v-if="m.action_proposal.token && !m.action_done" class="action-buttons">
                <button class="primary-btn" :disabled="actionBusy" @click="decideAction(m, 'confirm')">
                  {{ actionBusy ? '执行中…' : '确认执行' }}
                </button>
                <button class="ghost-btn" :disabled="actionBusy" @click="decideAction(m, 'cancel')">取消</button>
                <span class="muted">5 分钟内有效 · 确认后输出执行结果并四端留痕</span>
              </div>
              <p v-else-if="m.action_done || m.action_proposal.token" class="muted action-done">
                该确认已结束，如需再次执行请重新下达命令
              </p>
              <div class="msg-time muted">{{ m.created_at }}</div>
            </div>

            <!-- 执行输出 / 取消回执 -->
            <div v-else-if="m.action_result" class="result-card action-card">
              <div class="result-head">
                <span class="badge">{{ m.action_result.decision === 'confirm' ? '处置执行结果' : '处置已取消' }}</span>
                <span class="muted meta" v-if="m.action_result.result">
                  {{ m.action_result.result.finished_at }} · 目标 {{ m.action_result.result.host }}
                  · exit_code={{ m.action_result.result.exit_code }}
                </span>
              </div>
              <pre class="exec-out">{{ m.action_result.result?.stdout || m.content }}</pre>
              <p v-if="m.action_result.operation_code" class="muted action-done">
                工单 {{ m.action_result.operation_code }} 已生成，操作台账 / 工作日志 / 系统日志同步留痕
              </p>
              <div class="msg-time muted">{{ m.created_at }}</div>
            </div>

            <div v-else-if="m.structured" class="result-card">
              <div class="result-head">
                <span class="badge">{{ m.structured.intent === 'metrics' ? '指标查询结果' : (m.structured.intent === 'logs' ? '日志检索结果' : (m.structured.intent === 'trace' ? '链路定位结果' : (m.structured.intent === 'suggestion' ? '处置建议' : '排障结论'))) }}</span>
                <span class="muted meta">
                  置信度 {{ m.structured.confidence?.toFixed(2) }} · {{ m.structured.mode }}
                  · {{ m.structured.server }}
                </span>
              </div>
              <div class="result-block">
                <div class="label">{{ m.structured.intent === 'diagnose' ? '问题描述' : '结论' }}</div>
                <div class="value">{{ m.structured.problem }}</div>
              </div>
              <div class="result-block">
                <div class="label">位置</div>
                <div class="value">{{ m.structured.location }}</div>
              </div>
              <div class="result-grid">
                <div>
                  <div class="label">判断依据（真实数据）</div>
                  <ul><li v-for="(e, k) in m.structured.evidence" :key="k">{{ e }}</li></ul>
                </div>
                <div>
                  <div class="label">建议措施</div>
                  <ol><li v-for="(s, k) in m.structured.suggestions" :key="k">{{ s }}</li></ol>
                </div>
              </div>
              <div v-if="m.tool_trace?.length" class="tool-trace">
                <span class="muted">本次调用</span>
                <button v-for="(t, k) in m.tool_trace" :key="k" class="tool-chip"
                        title="点击查看该工具调用详情" @click="flash(toolDetail(t))">{{ t.tool }}</button>
              </div>
              <div class="msg-time muted">{{ m.created_at }}</div>
              <div v-if="canCreate" class="card-actions">
                <button class="ghost-btn" :disabled="creating" @click="createOperation(m)">
                  发起服务器处理
                </button>
                <span class="muted">生成工单后，四个角色端的工作台与日志会同步更新</span>
              </div>
            </div>
            <div v-else>
              <div class="plain-answer" :class="{ 'out-of-scope': m.in_scope === false }">
                {{ m.content }}
              </div>
              <div class="msg-time muted">{{ m.created_at }}</div>
            </div>
          </div>
        </div>
      </div>

      <div v-if="loading" class="msg-row assistant">
        <div class="assistant">
          <div class="assistant-avatar">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
              <path d="M4 15c3-7 8-9 16-8-1 8-5 12-11 11-2 0-4-1-5-3z" fill="#fff"/>
            </svg>
          </div>
          <div class="thinking">正在按问题调取对应数据…</div>
        </div>
      </div>
    </div>

    <div class="composer">
      <div class="quick">
        <button class="pill" @click="ask('0931 服务器为什么出现问题？')">典型排障用例</button>
        <button class="pill" @click="ask('0931 的磁盘使用率是多少？')">查指标</button>
        <button class="pill" @click="ask('再具体说说故障发生在哪个位置？')">追问详情</button>
        <button v-if="canExecute" class="pill" @click="ask('帮我清理内存')">直接处置</button>
        <button class="scope-pill" title="查看当前数据权限说明"
                @click="explainScope">{{ overviewOnly ? '概览模式' : '完整数据' }} ⓘ</button>
      </div>
      <div class="input-box">
        <el-input v-model="question" type="textarea" :rows="1" :autosize="{ minRows: 1, maxRows: 4 }"
                  resize="none" placeholder="描述服务器问题，例如：0931 服务器为什么出现问题？"
                  @keyup.enter.exact.prevent="ask()" />
        <button class="send" :disabled="loading || !question.trim()" @click="ask()">发送</button>
      </div>
      <p class="footnote">内容由 AI 生成，重要故障结论建议人工复核 · 仅支持运维排障专业问题</p>
    </div>
  </div>
</template>

<style scoped>
.chat { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.stream { flex: 1; overflow-y: auto; padding: 26px 24px 10px; }
.welcome { max-width: 620px; margin: 6vh auto 0; text-align: center; }
.welcome-logo { width: 58px; height: 58px; border-radius: 18px; background: var(--c-blue); display: grid; place-items: center; margin: 0 auto 26px; }
.welcome h1 { font-size: 21px; font-weight: 600; margin: 0 0 8px; }
.welcome p { font-size: 13px; margin: 0; }
.examples { margin-top: 30px; display: flex; flex-direction: column; gap: 9px; align-items: flex-start; }
.example {
  border: 1px solid var(--c-border); background: var(--c-white); border-radius: 12px;
  padding: 11px 16px; font-size: 13px; color: var(--c-text-2); cursor: pointer; text-align: left;
  transition: all .16s ease;
}
.example:hover { border-color: var(--c-blue); color: var(--c-blue); background: var(--c-blue-softer); }
.scope-tip { margin-top: 20px !important; color: var(--c-muted) !important; font-size: 12px !important; }
.domain-tip { margin-top: 8px !important; color: var(--c-muted) !important; font-size: 11px !important; }
.notice { max-width: 860px; margin: 0 auto 14px; background: var(--c-blue-soft); color: var(--c-blue-deep); border-radius: 10px; padding: 9px 14px; font-size: 12px; }

.msg-row { max-width: 860px; margin: 0 auto 18px; }
.msg-row.user { display: flex; justify-content: flex-end; }
.user-col { display: flex; flex-direction: column; align-items: flex-end; max-width: 72%; }
.bubble-user { background: var(--c-blue-soft); color: var(--c-blue-deep); border-radius: 16px 16px 4px 16px; padding: 11px 16px; font-size: 14px; line-height: 1.6; }
.msg-time { font-size: 10px; margin-top: 5px; text-align: right; }
.assistant-body .msg-time { text-align: left; }
.assistant { display: flex; gap: 10px; align-items: flex-start; }
.assistant-avatar { width: 28px; height: 28px; flex-shrink: 0; border-radius: 9px; background: var(--c-blue); display: grid; place-items: center; }
.assistant-body { flex: 1; min-width: 0; }
.result-card { border: 1px solid var(--c-border); border-radius: 16px; padding: 18px 20px; background: var(--c-white); }
.result-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 18px; }
.badge { background: var(--c-blue-soft); color: var(--c-blue); border-radius: 999px; padding: 4px 12px; font-size: 11px; font-weight: 500; }
.meta { font-size: 11px; }
.label { font-size: 11px; color: var(--c-muted); margin-bottom: 6px; }
.value { font-size: 14px; line-height: 1.7; color: var(--c-text); }
.result-block { margin-bottom: 16px; }
.result-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; border-top: 1px solid var(--c-border); padding-top: 16px; }
.result-grid ul, .result-grid ol { margin: 0; padding-left: 17px; font-size: 13px; line-height: 1.9; color: var(--c-text-2); }
.tool-trace { border-top: 1px solid var(--c-border); margin-top: 16px; padding-top: 12px; font-size: 11px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.tool-chip { border: 1px solid var(--c-blue-soft); color: var(--c-blue); background: var(--c-white); border-radius: 999px; padding: 2px 9px; font-size: 11px; cursor: pointer; font-family: inherit; }
.tool-chip:hover { background: var(--c-blue-soft); }
.card-actions { border-top: 1px solid var(--c-border); margin-top: 14px; padding-top: 13px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.card-actions .muted { font-size: 11px; }
.ghost-btn { border: 1px solid var(--c-blue); color: var(--c-blue); background: var(--c-white); border-radius: 999px; padding: 6px 15px; font-size: 12px; cursor: pointer; }
.ghost-btn:hover { background: var(--c-blue-softer); }
.ghost-btn:disabled { opacity: .55; cursor: not-allowed; }
.plain-answer { border: 1px solid var(--c-border); border-radius: 16px; padding: 14px 18px; white-space: pre-wrap; line-height: 1.8; font-size: 14px; overflow-wrap: anywhere; }
.plain-answer.out-of-scope { background: var(--c-bg); color: var(--c-text-2); border-style: dashed; }
.thinking { padding: 12px 16px; border: 1px solid var(--c-border); border-radius: 16px; color: var(--c-muted); font-size: 13px; }
.action-card { border-color: var(--c-blue-soft); }
.action-card .plain-answer { border: none; background: var(--c-bg); border-radius: 10px; font-size: 13px; line-height: 1.8; }
.action-buttons { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 13px; }
.action-buttons .muted { font-size: 11px; }
.primary-btn { border: 1px solid var(--c-blue); background: var(--c-blue); color: #fff; border-radius: 999px; padding: 6px 16px; font-size: 12px; cursor: pointer; }
.primary-btn:hover { opacity: .9; }
.primary-btn:disabled, .ghost-btn:disabled { opacity: .55; cursor: not-allowed; }
.action-done { font-size: 11px; margin: 12px 0 0; }
.exec-out { margin: 0; padding: 12px 14px; background: var(--c-bg); border: 1px solid var(--c-border); border-radius: 10px; font-size: 11px; line-height: 1.75; color: var(--c-text-2); white-space: pre-wrap; overflow-wrap: anywhere; max-height: 300px; overflow: auto; font-family: ui-monospace, Consolas, monospace; }

.composer { flex-shrink: 0; padding: 8px 24px 14px; }
.quick { max-width: 860px; margin: 0 auto 10px; display: flex; gap: 8px; align-items: center; }
.quick .pill { padding: 5px 12px; font-size: 11px; }
.scope-pill { margin-left: auto; font-size: 11px; color: var(--c-muted); border: none; background: transparent; cursor: pointer; font-family: inherit; padding: 4px 6px; }
.scope-pill:hover { color: var(--c-blue); }
.input-box {
  max-width: 860px; margin: 0 auto; display: flex; align-items: flex-end; gap: 10px;
  border: 1px solid var(--c-border); border-radius: 22px; padding: 10px 12px 10px 18px;
  background: var(--c-white);
}
.input-box:focus-within { border-color: var(--c-blue); }
.input-box :deep(.el-textarea__inner) { box-shadow: none; padding: 4px 0; resize: none; font-size: 14px; }
.send { border: none; background: var(--c-blue); color: #fff; border-radius: 999px; padding: 8px 20px; font-size: 13px; cursor: pointer; white-space: nowrap; }
.send:disabled { background: var(--c-border-strong); color: var(--c-muted); cursor: not-allowed; }
.footnote { text-align: center; color: var(--c-muted); font-size: 11px; margin: 10px 0 0; }

@media (max-width: 768px) {
  .stream { padding: 20px 14px 8px; }
  .welcome { margin-top: 9vh; }
  .welcome h1 { font-size: 19px; }
  .result-grid { grid-template-columns: 1fr; gap: 12px; }
  .composer { padding: 8px 12px 12px; }
  .quick { flex-wrap: wrap; }
}
</style>
