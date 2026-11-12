<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import LoginView from './views/LoginView.vue'
import ChatView from './views/ChatView.vue'
import ConsoleView from './views/ConsoleView.vue'
import OperationsView from './views/OperationsView.vue'
import PrivateChat from './views/PrivateChat.vue'
import api from './api'
import { DEMO_ROLES, ROLE_LABEL } from './accounts'
import { formatClock, syncClock } from './time'

const user = ref(null)
const view = ref('chat')
const conversations = ref([])
const sidebarOpen = ref(false)
const chatRef = ref(null)
const pendingCount = ref(0)
const openOpId = ref(null)
const sessionKey = ref(0)
const toast = ref('')
const confirmClear = ref(false)
const switchMenuOpen = ref(false)
const switching = ref(false)
const privateOpen = ref(false)
const privateThreadId = ref(null)
const unread = ref(0)
const clock = ref(formatClock())
let summaryTimer = null
let unreadTimer = null
let clockTimer = null

const perms = computed(() => new Set((user.value && user.value.permissions) || []))
const currentRole = computed(() => (user.value?.roles || [])[0] || '')
const roleLabel = computed(() => ROLE_LABEL[currentRole.value] || '')
const topTitle = computed(() => ({ chat: '新对话', operations: '工作台', console: '运维控制台' }[view.value] || ''))

function flash(text) {
  toast.value = text
  setTimeout(() => { toast.value = '' }, 3600)
}

function restore() {
  const raw = localStorage.getItem('opsai_user')
  if (raw && localStorage.getItem('opsai_token')) {
    try { user.value = JSON.parse(raw) } catch { user.value = null }
  }
}
function onUnauthorized() {
  user.value = null
  privateOpen.value = false
  switchMenuOpen.value = false
}

async function loadConversations() {
  if (!user.value) return
  try { conversations.value = await api.get('/chat/conversations') } catch { conversations.value = [] }
}
async function refreshSummary() {
  if (!user.value) return
  try {
    const s = await api.get('/operations/summary')
    pendingCount.value = (s.pending || 0) + (s.processing || 0)
  } catch { /* 无权限时忽略 */ }
}
async function refreshUnread() {
  if (!user.value) return
  try {
    const d = await api.get('/private/unread')
    unread.value = d.unread || 0
  } catch { /* 忽略瞬时失败 */ }
}
async function refreshClock() {
  const meta = await syncClock()
  clock.value = formatClock()
  return meta
}

function resetForUser(u) {
  user.value = u
  sessionKey.value += 1
  conversations.value = []
  pendingCount.value = 0
  openOpId.value = null
  unread.value = 0
  privateOpen.value = false
  privateThreadId.value = null
  switchMenuOpen.value = false
  sidebarOpen.value = false
  confirmClear.value = false
}

function onLoggedIn(u) {
  resetForUser(u)
  view.value = 'chat'
  loadConversations(); refreshSummary(); refreshUnread(); refreshClock()
}
function logout() {
  localStorage.removeItem('opsai_token')
  localStorage.removeItem('opsai_user')
  user.value = null
  conversations.value = []
  pendingCount.value = 0
  unread.value = 0
  privateOpen.value = false
  view.value = 'chat'
}

async function newChat() {
  view.value = 'chat'
  sidebarOpen.value = false
  await nextTick()
  if (chatRef.value) chatRef.value.newChat()
}
async function openConversation(id) {
  view.value = 'chat'
  sidebarOpen.value = false
  await nextTick()
  if (chatRef.value) await chatRef.value.loadConversation(id)
}
async function openOperation(id) {
  openOpId.value = null
  view.value = 'operations'
  sidebarOpen.value = false
  await nextTick()
  openOpId.value = id
}
function switchView(v) {
  view.value = v
  sidebarOpen.value = false
  switchMenuOpen.value = false
  if (v === 'operations') refreshSummary()
}

async function switchRole(role) {
  if (switching.value || role === currentRole.value) {
    switchMenuOpen.value = false
    return
  }
  switching.value = true
  try {
    const d = await api.post('/auth/demo-switch', { role })
    localStorage.setItem('opsai_token', d.access_token)
    localStorage.setItem('opsai_user', JSON.stringify(d.user))
    resetForUser(d.user)
    await Promise.all([loadConversations(), refreshSummary(), refreshUnread(), refreshClock()])
    flash(`已切换到「${ROLE_LABEL[role]}」端（${d.user.username}），对话记录已按账号隔离`)
  } catch (e) {
    flash(e.response?.data?.detail || '身份切换失败，请重新登录')
  } finally {
    switching.value = false
    switchMenuOpen.value = false
  }
}

async function clearHistory() {
  try {
    const d = await api.delete('/chat/history')
    conversations.value = []
    confirmClear.value = false
    await newChat()
    flash(`已清空本账号 ${d.deleted_conversations} 条对话记录`)
  } catch (e) {
    flash(e.response?.data?.detail || '清空失败')
  }
}

function onChanged() { loadConversations(); refreshSummary() }

onMounted(() => {
  restore()
  window.addEventListener('opsai-unauthorized', onUnauthorized)
  if (user.value) { loadConversations(); refreshSummary(); refreshUnread(); refreshClock() }
  summaryTimer = setInterval(refreshSummary, 10000)
  unreadTimer = setInterval(refreshUnread, 5000)
  clockTimer = setInterval(() => { clock.value = formatClock() }, 1000)
})
onUnmounted(() => {
  window.removeEventListener('opsai-unauthorized', onUnauthorized)
  if (summaryTimer) clearInterval(summaryTimer)
  if (unreadTimer) clearInterval(unreadTimer)
  if (clockTimer) clearInterval(clockTimer)
})
</script>

<template>
  <LoginView v-if="!user" @login="onLoggedIn" />

  <div v-else class="shell">
    <div v-if="sidebarOpen" class="mask" @click="sidebarOpen = false"></div>
    <div v-if="switchMenuOpen" class="menu-mask" @click="switchMenuOpen = false"></div>

    <aside class="sidebar" :class="{ open: sidebarOpen }">
      <div class="brand">
        <span class="logo">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
            <path d="M4 15c3-7 8-9 16-8-1 8-5 12-11 11-2 0-4-1-5-3z" fill="#fff"/>
            <path d="M7 16c3-3 6-4 9-4" stroke="#4c6fff" stroke-width="1.6" stroke-linecap="round"/>
          </svg>
        </span>
        <span>智能排障</span>
      </div>

      <button class="new-chat" @click="newChat"><span class="plus">＋</span> 新对话</button>

      <nav class="nav">
        <button :class="{ active: view === 'chat' }" @click="switchView('chat')">对话排障</button>
        <button :class="{ active: view === 'operations' }" @click="switchView('operations')">
          工作台<span v-if="pendingCount" class="badge-num">{{ pendingCount }}</span>
        </button>
        <button :class="{ active: view === 'console' }" @click="switchView('console')">运维控制台</button>
      </nav>

      <div class="section-title history-title">
        <span>最近对话（仅本账号）</span>
        <span class="clear-actions">
          <button v-if="!confirmClear" class="clear-btn" @click="confirmClear = true">清空</button>
          <template v-else>
            <button class="clear-btn danger" @click="clearHistory">确认</button>
            <button class="clear-btn" @click="confirmClear = false">取消</button>
          </template>
        </span>
      </div>
      <div class="history">
        <button v-for="c in conversations" :key="c.id" class="history-item" @click="openConversation(c.id)">
          {{ c.title }}
        </button>
        <p v-if="!conversations.length" class="muted empty">暂无对话记录</p>
      </div>

      <div class="divider"></div>
      <div class="user-row">
        <div>
          <div class="uname">{{ user.display_name || user.username }}</div>
          <div class="muted role">{{ roleLabel }} · {{ user.username }}</div>
        </div>
        <button class="logout" @click="logout">退出</button>
      </div>
    </aside>

    <main class="main">
      <header class="topbar">
        <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
        <span class="top-title">{{ topTitle }}</span>
        <div class="top-right">
          <span class="clock" title="与服务器本地时间同步">{{ clock }}</span>
          <button v-if="pendingCount" class="pending-tag" title="查看待办工单"
                  @click="switchView('operations')">待办 {{ pendingCount }}</button>
          <button class="private-btn" @click="privateOpen = true">
            私聊<span v-if="unread" class="badge-num">{{ unread }}</span>
          </button>
          <div class="switch-wrap">
            <button class="switch-btn" :disabled="switching" @click="switchMenuOpen = !switchMenuOpen">
              {{ switching ? '切换中…' : roleLabel }} <span class="caret">⌄</span>
            </button>
            <div v-if="switchMenuOpen" class="switch-menu">
              <div class="menu-title">随时切换四端身份</div>
              <button v-for="r in DEMO_ROLES" :key="r.role" class="menu-item"
                      :class="{ on: r.role === currentRole }" @click="switchRole(r.role)">
                <span class="mi-label">{{ r.label }}<span v-if="r.role === currentRole" class="mi-now">当前</span></span>
                <span class="mi-desc">{{ r.username }} · {{ r.desc }}</span>
              </button>
            </div>
          </div>
          <button class="top-new" title="新建对话" @click="newChat">＋</button>
        </div>
      </header>

      <p v-if="toast" class="global-toast">{{ toast }}</p>

      <ChatView v-if="view === 'chat'" :key="'chat-' + sessionKey" ref="chatRef" :user="user"
                @changed="onChanged" @open-operation="openOperation" />
      <OperationsView v-else-if="view === 'operations'" :key="'ops-' + sessionKey" :user="user"
                      :open-id="openOpId" @changed="onChanged" />
      <ConsoleView v-else :key="'console-' + sessionKey" :user="user" @changed="onChanged" />
    </main>

    <PrivateChat :key="'pc-' + (user.id || 0)" :user="user" :open="privateOpen"
                 :thread-id="privateThreadId" @close="privateOpen = false"
                 @unread="unread = $event" />
  </div>
</template>

<style scoped>
.shell { display: flex; height: 100vh; background: var(--c-white); }
.sidebar {
  width: 236px; flex-shrink: 0; background: var(--c-bg); border-right: 1px solid var(--c-border);
  display: flex; flex-direction: column; padding: 22px 14px 16px;
}
.brand { display: flex; align-items: center; gap: 9px; font-size: 15px; font-weight: 600; padding: 0 8px 22px; }
.logo { width: 30px; height: 30px; border-radius: 10px; background: var(--c-blue); display: grid; place-items: center; }
.new-chat {
  width: 100%; border: 1px solid var(--c-blue); background: var(--c-white); color: var(--c-blue);
  border-radius: 11px; padding: 10px; font-size: 13px; cursor: pointer; margin-bottom: 20px;
}
.new-chat:hover { background: var(--c-blue-soft); }
.plus { font-size: 15px; margin-right: 4px; }
.nav { display: flex; flex-direction: column; gap: 3px; }
.nav button {
  text-align: left; border: none; background: transparent; padding: 9px 12px; border-radius: 9px;
  font-size: 13px; color: var(--c-text-2); cursor: pointer; display: flex; align-items: center; gap: 8px;
}
.nav button:hover { background: var(--c-white); }
.nav button.active { background: var(--c-blue-soft); color: var(--c-blue); font-weight: 500; }
.badge-num { margin-left: auto; background: var(--c-blue); color: #fff; border-radius: 999px; min-width: 18px; height: 18px; display: grid; place-items: center; font-size: 10px; padding: 0 5px; }
.history-title { display: flex; align-items: center; justify-content: space-between; gap: 6px; padding: 20px 10px 8px; color: var(--c-muted); font-weight: 400; font-size: 12px; }
.clear-actions { display: inline-flex; gap: 4px; }
.clear-btn { border: none; background: transparent; color: var(--c-blue); font-size: 11px; cursor: pointer; padding: 2px 4px; }
.clear-btn:hover { text-decoration: underline; }
.clear-btn.danger { color: #d94b4b; }
.history { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; }
.history-item {
  text-align: left; border: none; background: transparent; padding: 8px 10px; border-radius: 8px;
  font-size: 12px; color: var(--c-text-2); cursor: pointer;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.history-item:hover { background: var(--c-white); color: var(--c-blue); }
.empty { font-size: 12px; padding: 4px 10px; }
.user-row { display: flex; align-items: center; justify-content: space-between; padding: 14px 8px 0; }
.uname { font-size: 13px; font-weight: 500; }
.role { font-size: 11px; margin-top: 2px; }
.logout { border: none; background: transparent; color: var(--c-muted); font-size: 12px; cursor: pointer; }
.logout:hover { color: var(--c-blue); }

.main { flex: 1; min-width: 0; display: flex; flex-direction: column; position: relative; }
.topbar {
  height: 58px; flex-shrink: 0; border-bottom: 1px solid var(--c-border);
  display: flex; align-items: center; justify-content: space-between; padding: 0 22px; position: relative;
}
.top-title { position: absolute; left: 50%; transform: translateX(-50%); font-size: 15px; font-weight: 500; }
.top-right { display: flex; align-items: center; gap: 8px; margin-left: auto; }
.clock { font-size: 11px; color: var(--c-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.pending-tag { border: none; background: var(--c-blue-soft); color: var(--c-blue); border-radius: 999px; padding: 5px 11px; font-size: 11px; cursor: pointer; white-space: nowrap; }
.pending-tag:hover { background: var(--c-blue); color: #fff; }
.private-btn { border: 1px solid var(--c-border); background: var(--c-white); color: var(--c-text-2); border-radius: 999px; padding: 5px 12px; font-size: 11px; cursor: pointer; display: flex; align-items: center; gap: 5px; white-space: nowrap; }
.private-btn:hover { border-color: var(--c-blue); color: var(--c-blue); }
.private-btn .badge-num { margin-left: 0; }
.switch-wrap { position: relative; }
.switch-btn { border: 1px solid var(--c-border); background: var(--c-white); color: var(--c-text-2); border-radius: 999px; padding: 5px 12px; font-size: 11px; cursor: pointer; white-space: nowrap; }
.switch-btn:hover { border-color: var(--c-blue); color: var(--c-blue); }
.switch-btn:disabled { opacity: .6; cursor: wait; }
.caret { margin-left: 3px; }
.switch-menu { position: absolute; top: 36px; right: 0; width: 246px; background: var(--c-white); border: 1px solid var(--c-border); border-radius: 13px; box-shadow: 0 12px 34px rgba(29, 33, 41, .12); padding: 8px; z-index: 30; }
.menu-title { font-size: 11px; color: var(--c-muted); padding: 6px 10px 8px; }
.menu-item { width: 100%; text-align: left; border: none; background: transparent; border-radius: 9px; padding: 9px 10px; cursor: pointer; display: flex; flex-direction: column; gap: 3px; }
.menu-item:hover { background: var(--c-blue-softer); }
.menu-item.on { background: var(--c-blue-soft); }
.mi-label { font-size: 13px; font-weight: 500; color: var(--c-text); }
.mi-now { margin-left: 8px; font-size: 10px; color: var(--c-blue); font-weight: 400; }
.mi-desc { font-size: 10px; color: var(--c-muted); }
.menu-mask { position: fixed; inset: 0; z-index: 25; }
.top-new, .menu-btn {
  width: 30px; height: 30px; border: 1px solid var(--c-border); border-radius: 50%;
  background: var(--c-white); cursor: pointer; font-size: 16px; color: var(--c-text-2); line-height: 1;
}
.top-new:hover, .menu-btn:hover { border-color: var(--c-blue); color: var(--c-blue); }
.menu-btn { display: none; }
.mask { display: none; }
.global-toast { position: absolute; top: 66px; right: 22px; z-index: 12; margin: 0; background: var(--c-blue-soft); color: var(--c-blue-deep); border-radius: 10px; padding: 9px 14px; font-size: 12px; box-shadow: 0 6px 20px rgba(29,33,41,.08); }

@media (max-width: 768px) {
  .sidebar { position: fixed; left: 0; top: 0; bottom: 0; z-index: 20; transform: translateX(-100%); transition: transform .2s ease; }
  .sidebar.open { transform: translateX(0); }
  .mask { display: block; position: fixed; inset: 0; background: rgba(20, 28, 45, .18); z-index: 15; }
  .menu-btn { display: block; }
  .topbar { padding: 0 12px; }
  .top-title { display: none; }
  .clock { display: none; }
  .pending-tag { display: none; }
  .global-toast { right: 12px; left: 12px; }
}
</style>
