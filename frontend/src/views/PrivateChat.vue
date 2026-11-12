<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import api from '../api'

const props = defineProps({ user: Object, open: Boolean, threadId: Number })
const emit = defineEmits(['close', 'unread'])

const isAdmin = computed(() => (props.user?.permissions || []).includes('inquiry.view'))
const tab = ref('threads')
const threads = ref([])
const inquiries = ref([])
const activeId = ref(null)
const thread = ref(null)
const messages = ref([])
const draft = ref('')
const notice = ref('')
const busy = ref(false)
const scroller = ref(null)
let timer = null
let refreshing = false
let lastMsgId = 0

function flash(text) {
  notice.value = text
  setTimeout(() => { notice.value = '' }, 3500)
}

async function scrollBottom() {
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
}

async function loadUnread() {
  try {
    const d = await api.get('/private/unread')
    emit('unread', d.unread || 0)
  } catch { /* 忽略瞬时失败 */ }
}

async function loadThreads() {
  try { threads.value = await api.get('/private/threads') } catch { /* 忽略 */ }
}

async function loadInquiries() {
  if (!isAdmin.value) return
  try { inquiries.value = await api.get('/admin/inquiries') } catch { /* 忽略 */ }
}

async function loadMessages(id, force = false) {
  try {
    const d = await api.get(`/private/threads/${id}/messages`)
    thread.value = d.thread
    messages.value = d.messages || []
    const newest = messages.value.length ? messages.value[messages.value.length - 1].id : 0
    if (force || newest !== lastMsgId) { lastMsgId = newest; await scrollBottom() }
  } catch (e) {
    flash(e.response?.data?.detail || '消息加载失败')
  }
}

async function refresh() {
  if (refreshing) return
  refreshing = true
  try {
    await Promise.all([
      loadUnread(),
      loadThreads(),
      loadInquiries(),
      activeId.value ? loadMessages(activeId.value) : Promise.resolve(),
    ])
  } finally { refreshing = false }
}

async function openThread(id) {
  activeId.value = id
  tab.value = 'threads'
  lastMsgId = 0
  await loadMessages(id, true)
  await loadUnread()
}

async function startChat(row) {
  if (row.thread_id) { await openThread(row.thread_id); return }
  busy.value = true
  try {
    const d = await api.post('/private/threads', {
      target_user_id: row.id,
      subject: '排障问题沟通',
    })
    await loadThreads()
    await loadInquiries()
    await openThread(d.id)
    flash(`已与 ${row.display_name}（${row.role_label}）建立一对一私聊`)
  } catch (e) {
    flash(e.response?.data?.detail || '发起私聊失败')
  } finally { busy.value = false }
}

async function send() {
  const text = draft.value.trim()
  if (!text || !activeId.value || busy.value) return
  busy.value = true
  draft.value = ''
  try {
    await api.post(`/private/threads/${activeId.value}/messages`, { content: text })
    await loadMessages(activeId.value, true)
    await loadThreads()
    await loadUnread()
  } catch (e) {
    draft.value = text
    flash(e.response?.data?.detail || '发送失败，请重试')
  } finally { busy.value = false }
}

function back() {
  activeId.value = null
  thread.value = null
  messages.value = []
  tab.value = 'threads'
  loadThreads()
}

function close() { emit('close') }
function switchTab(t) { tab.value = t; activeId.value = null; thread.value = null }

watch(() => props.open, (v) => {
  if (v) {
    refresh()
    if (!timer) timer = setInterval(refresh, 3000)
  } else if (timer) {
    clearInterval(timer); timer = null
  }
})
watch(() => props.threadId, (v) => { if (v && props.open) openThread(v) })
watch(() => props.user?.id, () => {
  activeId.value = null; thread.value = null; messages.value = []
  threads.value = []; inquiries.value = []
  if (props.open) refresh()
})
onMounted(() => { if (props.open) { refresh(); timer = setInterval(refresh, 3000) } })
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div v-if="open" class="chat-layer">
    <div class="drawer-mask" @click="close"></div>
    <aside class="drawer">
      <header class="d-head">
        <div>
          <div class="d-title">一对一沟通</div>
          <div class="d-sub muted">{{ isAdmin ? '查看提问人身份并发起私聊' : '与管理员的实时私聊' }}</div>
        </div>
        <button class="icon-x" title="关闭" @click="close">✕</button>
      </header>

      <p v-if="notice" class="notice">{{ notice }}</p>

      <div class="d-tabs">
        <button :class="{ on: tab === 'threads' }" @click="switchTab('threads')">
          会话<span v-if="threads.length" class="mini">{{ threads.length }}</span>
        </button>
        <button v-if="isAdmin" :class="{ on: tab === 'inquiries' }" @click="switchTab('inquiries')">
          提问人身份
        </button>
      </div>

      <!-- 会话详情 -->
      <template v-if="tab === 'threads' && activeId">
        <div class="conv-head">
          <button class="back" @click="back">← 返回</button>
          <div v-if="thread?.other" class="who">
            <b>{{ thread.other.display_name }}</b>
            <span class="muted">{{ thread.other.username }} · {{ thread.other.role_label }}</span>
          </div>
        </div>
        <div ref="scroller" class="msg-scroll">
          <div v-for="m in messages" :key="m.id" class="msg" :class="{ mine: m.mine }">
            <div class="bubble">{{ m.content }}</div>
            <div class="msg-meta muted">{{ m.mine ? '我' : m.sender_name }} · {{ m.created_at }}</div>
          </div>
          <p v-if="!messages.length" class="muted empty">还没有消息，发送第一条试试</p>
        </div>
        <div class="composer">
          <textarea v-model="draft" rows="2" placeholder="输入消息，回车发送…"
                    @keyup.enter.exact.prevent="send"></textarea>
          <button class="btn-send" :disabled="busy || !draft.trim()" @click="send">发送</button>
        </div>
      </template>

      <!-- 会话列表 -->
      <div v-else-if="tab === 'threads'" class="d-body">
        <button v-for="t in threads" :key="t.id" class="thread-card" @click="openThread(t.id)">
          <div class="tc-top">
            <b>{{ t.other?.display_name || '未知用户' }}</b>
            <span v-if="t.unread" class="unread">{{ t.unread }}</span>
            <span class="muted time">{{ t.updated_at }}</span>
          </div>
          <div class="muted tc-sub">{{ t.other?.username }} · {{ t.other?.role_label }} · {{ t.subject }}</div>
          <div class="tc-last">{{ t.last_message || '暂无消息' }}</div>
        </button>
        <p v-if="!threads.length" class="muted empty">
          {{ isAdmin ? '还没有私聊会话，可到「提问人身份」发起' : '管理员发起私聊后，会话会出现在这里' }}
        </p>
      </div>

      <!-- 管理员：提问人身份 -->
      <div v-else class="d-body">
        <p class="hint muted">仅显示身份与提问摘要，四端对话正文彼此隔离</p>
        <div v-for="row in inquiries" :key="row.id" class="inq-card">
          <div class="inq-head">
            <b>{{ row.display_name }}</b>
            <span class="role-chip">{{ row.role_label }}</span>
            <span v-if="row.is_self" class="muted self">当前账号</span>
          </div>
          <div class="inq-kv muted">账号 {{ row.username }} · 提问 {{ row.question_count }} 次 · 对话 {{ row.conversation_count }} 个</div>
          <div class="inq-kv muted">最近来源 IP {{ row.last_ip || '—' }} · {{ row.last_question_at || '暂无提问' }}</div>
          <div v-if="row.last_question" class="inq-q">最近提问：{{ row.last_question }}</div>
          <button v-if="!row.is_self" class="ghost-btn" :disabled="busy" @click="startChat(row)">
            {{ row.thread_id ? '进入私聊' : '发起私聊' }}
          </button>
        </div>
        <p v-if="!inquiries.length" class="muted empty">暂无账号信息</p>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.chat-layer { position: fixed; inset: 0; z-index: 40; }
.drawer-mask { position: absolute; inset: 0; background: rgba(20, 28, 45, .16); }
.drawer {
  position: absolute; right: 0; top: 0; bottom: 0; width: 400px; max-width: 100%;
  background: var(--c-white); border-left: 1px solid var(--c-border);
  display: flex; flex-direction: column; box-shadow: -8px 0 30px rgba(29, 33, 41, .08);
}
.d-head { display: flex; align-items: flex-start; justify-content: space-between; padding: 18px 20px 14px; border-bottom: 1px solid var(--c-border); }
.d-title { font-size: 15px; font-weight: 600; }
.d-sub { font-size: 11px; margin-top: 4px; }
.icon-x { width: 28px; height: 28px; border: 1px solid var(--c-border); border-radius: 50%; background: var(--c-white); color: var(--c-text-2); cursor: pointer; font-size: 12px; }
.icon-x:hover { border-color: var(--c-blue); color: var(--c-blue); }
.notice { margin: 10px 16px 0; background: var(--c-blue-soft); color: var(--c-blue-deep); border-radius: 9px; padding: 8px 12px; font-size: 12px; }
.d-tabs { display: flex; gap: 4px; padding: 10px 16px; border-bottom: 1px solid var(--c-border); }
.d-tabs button { border: none; background: transparent; padding: 7px 14px; border-radius: 9px; font-size: 12px; color: var(--c-text-2); cursor: pointer; }
.d-tabs button:hover { background: var(--c-bg); }
.d-tabs button.on { background: var(--c-blue-soft); color: var(--c-blue); font-weight: 500; }
.mini { margin-left: 6px; background: var(--c-bg); border-radius: 999px; padding: 1px 6px; font-size: 10px; }
.d-body { flex: 1; overflow-y: auto; padding: 14px 16px 24px; }
.hint { font-size: 11px; margin: 0 0 12px; line-height: 1.6; }
.empty { text-align: center; font-size: 12px; padding: 40px 12px; }
.thread-card, .inq-card { width: 100%; display: block; text-align: left; border: 1px solid var(--c-border); background: var(--c-white); border-radius: 12px; padding: 13px 14px; margin-bottom: 10px; cursor: pointer; }
.thread-card:hover { border-color: var(--c-blue); background: var(--c-blue-softer); }
.tc-top { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.tc-top .time { margin-left: auto; font-size: 10px; }
.unread { background: var(--c-blue); color: #fff; border-radius: 999px; min-width: 17px; height: 17px; display: grid; place-items: center; font-size: 10px; padding: 0 5px; }
.tc-sub { font-size: 11px; margin-top: 5px; }
.tc-last { font-size: 12px; color: var(--c-text-2); margin-top: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.inq-card { cursor: default; }
.inq-card:hover { border-color: var(--c-border); background: var(--c-white); }
.inq-head { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.role-chip { background: var(--c-blue-soft); color: var(--c-blue); border-radius: 999px; padding: 2px 9px; font-size: 10px; }
.self { margin-left: auto; font-size: 11px; }
.inq-kv { font-size: 11px; margin-top: 7px; line-height: 1.5; }
.inq-q { margin-top: 9px; background: var(--c-bg); border-radius: 8px; padding: 8px 10px; font-size: 11px; color: var(--c-text-2); line-height: 1.6; }
.ghost-btn { margin-top: 11px; border: 1px solid var(--c-blue); color: var(--c-blue); background: var(--c-white); border-radius: 999px; padding: 6px 15px; font-size: 12px; cursor: pointer; }
.ghost-btn:hover { background: var(--c-blue-softer); }
.ghost-btn:disabled { opacity: .55; cursor: not-allowed; }
.conv-head { display: flex; align-items: center; gap: 12px; padding: 12px 16px; border-bottom: 1px solid var(--c-border); }
.back { border: none; background: transparent; color: var(--c-blue); font-size: 12px; cursor: pointer; padding: 0; }
.who { display: flex; flex-direction: column; gap: 3px; font-size: 13px; }
.who .muted { font-size: 11px; }
.msg-scroll { flex: 1; overflow-y: auto; padding: 18px 16px; background: var(--c-bg); }
.msg { margin-bottom: 14px; display: flex; flex-direction: column; align-items: flex-start; }
.msg.mine { align-items: flex-end; }
.bubble { max-width: 78%; border-radius: 14px 14px 14px 4px; padding: 9px 13px; font-size: 13px; line-height: 1.65; background: var(--c-white); border: 1px solid var(--c-border); white-space: pre-wrap; word-break: break-word; }
.msg.mine .bubble { background: var(--c-blue); color: #fff; border-color: var(--c-blue); border-radius: 14px 14px 4px 14px; }
.msg-meta { font-size: 10px; margin-top: 5px; }
.composer { border-top: 1px solid var(--c-border); padding: 12px 16px 16px; display: flex; gap: 10px; align-items: flex-end; }
.composer textarea { flex: 1; resize: none; border: 1px solid var(--c-border); border-radius: 11px; padding: 9px 11px; font-family: inherit; font-size: 13px; color: var(--c-text); outline: none; }
.composer textarea:focus { border-color: var(--c-blue); }
.btn-send { border: none; background: var(--c-blue); color: #fff; border-radius: 999px; padding: 9px 17px; font-size: 12px; cursor: pointer; white-space: nowrap; }
.btn-send:disabled { background: var(--c-border-strong); color: var(--c-muted); cursor: not-allowed; }
@media (max-width: 480px) { .drawer { width: 100%; } }
</style>
