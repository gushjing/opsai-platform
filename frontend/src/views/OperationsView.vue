<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import api from '../api'

const props = defineProps({ user: Object, openId: Number })
const emit = defineEmits(['changed'])

const perms = computed(() => new Set((props.user && props.user.permissions) || []))
const has = (p) => perms.value.has(p)
const canHandle = computed(() => has('operation.handle'))
const canExecute = computed(() => has('operation.handle') && has('ops.execute'))

const tab = ref('records')
const records = ref([])
const activity = ref([])
const summary = ref({ pending: 0, processing: 0, total: 0 })
const selectedId = ref(null)
const detail = ref(null)
const statusFilter = ref('')
const notice = ref('')
const busy = ref(false)

const newForm = ref({ server: '0931', title: '', issue: '' })
const showNew = ref(false)
const logText = ref('')
const requestAction = ref('')
const execCommand = ref('')
const confirmToken = ref('')
const execResult = ref(null)
let timer = null

const statusClass = (s) => ({ pending: 's-pending', processing: 's-doing', resolved: 's-done', closed: 's-closed' }[s] || 's-closed')

// 工单号很长，列表里只留可辨认的首尾，完整值放在 title 里，绝不撑破卡片
function shortCode(code) {
  if (!code) return ''
  return code.length > 16 ? `${code.slice(0, 3)}…${code.slice(-4)}` : code
}

const NOISE_ACTIONS = new Set(['request.write', 'auth.login'])

function flash(text) {
  notice.value = text
  setTimeout(() => { notice.value = '' }, 4000)
}

async function loadAll() {
  try {
    const qs = statusFilter.value ? `?status=${statusFilter.value}` : ''
    records.value = await api.get(`/operations${qs}`)
    summary.value = await api.get('/operations/summary')
    if (has('activity.view')) {
      const rows = await api.get('/activity?limit=60')
      // 兜底访问日志已在后端剔除，这里再做一层保险，动态流只保留业务动作
      activity.value = rows.filter((a) => !NOISE_ACTIONS.has(a.raw_action || a.action))
    }
    if (selectedId.value) await loadDetail(selectedId.value, true)
  } catch (e) {
    flash(e.response?.data?.detail || '数据刷新失败')
  }
}

async function loadDetail(id, silent = false) {
  selectedId.value = id
  try {
    detail.value = await api.get(`/operations/${id}`)
    if (!execCommand.value && detail.value.record.pending_action) {
      execCommand.value = detail.value.record.pending_action
    }
  } catch (e) {
    flash(e.response?.data?.detail || '无权查看该工单')
    detail.value = null
  }
}

async function scrollDetail() {
  // 详情面板自身滚动，不干扰列表
}

async function createOperation() {
  if (!newForm.value.title.trim()) { flash('请填写处理标题'); return }
  busy.value = true
  try {
    const d = await api.post('/operations', {
      server: newForm.value.server,
      title: newForm.value.title.trim(),
      issue: newForm.value.issue.trim() || '由工作台发起的服务器处理',
    })
    newForm.value = { server: '0931', title: '', issue: '' }
    showNew.value = false
    await loadAll()
    await loadDetail(d.record.id)
    emit('changed')
    flash(d.message || `已发起处理工单 ${d.record.code}，操作与工作日志均已留痕`)
  } catch (e) { flash(e.response?.data?.detail || '发起失败') }
  finally { busy.value = false }
}

async function appendLog(withAction) {
  const content = logText.value.trim()
  if (!content) { flash('请填写工作日志内容'); return }
  busy.value = true
  try {
    const d = await api.post(`/operations/${selectedId.value}/worklogs`, {
      content,
      request_action: withAction ? (requestAction.value.trim() || content) : '',
    })
    logText.value = ''
    requestAction.value = ''
    detail.value = d
    await loadAll()
    emit('changed')
    flash(withAction ? '处理请求已提交，工程师端可接单执行' : '工作日志已更新，操作台账与审计日志同步留痕')
  } catch (e) { flash(e.response?.data?.detail || '更新失败') }
  finally { busy.value = false }
}

async function handle(status) {
  busy.value = true
  try {
    const d = await api.post(`/operations/${selectedId.value}/handle`, {
      status, note: '',
    })
    detail.value = d
    await loadAll()
    emit('changed')
    flash(`状态已更新为「${d.record.status_label}」`)
  } catch (e) { flash(e.response?.data?.detail || '流转失败') }
  finally { busy.value = false }
}

async function execute(withConfirm) {
  const command = execCommand.value.trim()
  if (!command) { flash('请填写要执行的命令'); return }
  busy.value = true
  try {
    const payload = { command }
    if (withConfirm) payload.confirm_token = confirmToken.value
    const d = await api.post(`/operations/${selectedId.value}/execute`, payload)
    if (d.need_confirm) {
      confirmToken.value = d.confirm_token
      execResult.value = null
      flash(d.warning)
    } else {
      confirmToken.value = ''
      execResult.value = d
      detail.value = {
        record: d.record, worklogs: d.worklogs, related_audits: d.related_audits,
      }
      await loadAll()
      emit('changed')
      flash('命令已执行，工单自动置为已解决并写入工作日志')
    }
  } catch (e) { flash(e.response?.data?.detail || '执行失败') }
  finally { busy.value = false }
}

async function openFromActivity(item) {
  // 后端只在本账号确实有权打开该工单时返回 record_id，因此这里点开必定有内容
  if (!item.record_id) return
  tab.value = 'records'
  statusFilter.value = ''
  await loadAll()
  await loadDetail(item.record_id)
  flash(`已打开工单 ${item.record_code}`)
}

watch(() => props.openId, (v) => { if (v) { tab.value = 'records'; loadDetail(v) } })
watch(statusFilter, () => loadAll())
onMounted(() => { loadAll(); timer = setInterval(loadAll, 8000) })
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="ops">
    <div class="ops-inner">
      <div class="head-row">
        <div>
          <h2>工作台</h2>
          <p class="muted">四端共享的操作台账 · 每次动作同步写入操作记录、工作日志与系统日志</p>
        </div>
        <button v-if="has('operation.create')" class="btn-primary" @click="showNew = !showNew">
          {{ showNew ? '收起' : '＋ 发起服务器处理' }}
        </button>
      </div>

      <p v-if="notice" class="notice">{{ notice }}</p>

      <div v-if="showNew" class="soft-card new-panel">
        <div class="section-title">新建处理工单</div>
        <div class="form-row">
          <el-input v-model="newForm.server" placeholder="服务器编号" style="max-width: 150px" />
          <el-input v-model="newForm.title" placeholder="处理标题，如 0931 磁盘清理" style="max-width: 260px" />
          <el-input v-model="newForm.issue" placeholder="问题描述（可选）" style="max-width: 300px" />
          <el-button type="primary" :loading="busy" @click="createOperation">提交</el-button>
        </div>
      </div>

      <div class="tabs">
        <button :class="{ on: tab === 'records' }" @click="tab = 'records'">操作台账</button>
        <button v-if="has('activity.view')" :class="{ on: tab === 'activity' }" @click="tab = 'activity'">
          实时动态
        </button>
        <span v-if="tab === 'records'" class="spacer"></span>
        <select v-if="tab === 'records'" v-model="statusFilter" class="filter">
          <option value="">全部状态</option>
          <option value="pending">待处理</option>
          <option value="processing">处理中</option>
          <option value="resolved">已解决</option>
          <option value="closed">已关闭</option>
        </select>
        <span class="counts">待处理 {{ summary.pending }} · 处理中 {{ summary.processing }} · 共 {{ summary.total }}</span>
      </div>

      <!-- 操作台账 -->
      <div v-if="tab === 'records'" class="split">
        <div class="list-col">
          <div v-if="!records.length" class="soft-card empty-card">暂无操作记录，可从对话结论或右上角发起处理</div>
          <button v-for="r in records" :key="r.id" class="rec-card"
                  :class="{ selected: selectedId === r.id }" @click="loadDetail(r.id)">
            <div class="rec-top">
              <span class="code truncate" :title="r.code">{{ shortCode(r.code) }}</span>
              <span class="status" :class="statusClass(r.status)">{{ r.status_label }}</span>
            </div>
            <div class="rec-title truncate" :title="r.title">{{ r.title }}</div>
            <div class="rec-meta">
              <span>{{ r.server }}</span><span>·</span>
              <span class="truncate" :title="r.reporters || r.requester">
                {{ r.reporter_count > 1 ? r.reporter_count + ' 人报修' : (r.requester || '—') }}
              </span>
              <span>·</span><span>{{ r.worklog_count }} 条日志</span>
            </div>
            <div v-if="r.latest_log" class="rec-last">{{ r.latest_log.username }}：{{ r.latest_log.content }}</div>
          </button>
        </div>

        <div class="detail-col">
          <div v-if="!detail" class="soft-card empty-card">选择左侧工单查看工作日志时间线</div>
          <template v-else>
            <div class="soft-card detail-head">
              <div class="detail-title-row">
                <div>
                  <div class="code code-full" :title="detail.record.code">{{ detail.record.code }}</div>
                  <h3>{{ detail.record.title }}</h3>
                </div>
                <span class="status" :class="statusClass(detail.record.status)">{{ detail.record.status_label }}</span>
              </div>
              <div class="kv-grid">
                <div><span class="muted">目标服务器</span><b>{{ detail.record.server }}</b></div>
                <div>
                  <span class="muted">报修人{{ detail.record.reporter_count > 1 ? '（共 ' + detail.record.reporter_count + ' 人）' : '' }}</span>
                  <b class="reporters clamp-2" :title="detail.record.reporters || detail.record.requester">
                    {{ detail.record.reporters || detail.record.requester }}
                  </b>
                </div>
                <div><span class="muted">处理人</span><b>{{ detail.record.assignee || '未接单' }}</b></div>
                <div><span class="muted">最近动作</span><b>{{ detail.record.last_action || '—' }}</b></div>
              </div>
              <div v-if="detail.record.issue" class="issue">{{ detail.record.issue }}</div>
              <div v-if="detail.record.pending_action" class="pending-action">
                待执行处理请求：{{ detail.record.pending_action }}
              </div>

              <div class="action-bar">
                <el-button v-if="canHandle && detail.record.status === 'pending'" size="small" type="primary"
                           :loading="busy" @click="handle('processing')">接单处理</el-button>
                <el-button v-if="canHandle && detail.record.status === 'processing'" size="small" type="primary"
                           :loading="busy" @click="handle('resolved')">标记已解决</el-button>
                <el-button v-if="canHandle && detail.record.status !== 'closed'" size="small"
                           :loading="busy" @click="handle('closed')">关闭工单</el-button>
                <span v-if="!canHandle" class="muted tip-inline">当前角色可追加工作日志 / 提交处理请求</span>
              </div>
            </div>

            <!-- 工作日志时间线 -->
            <div class="soft-card">
              <div class="section-title">工作日志时间线（{{ detail.worklogs.length }}）</div>
              <div class="timeline">
                <div v-for="w in detail.worklogs" :key="w.id" class="tl-item">
                  <span class="dot"></span>
                  <div class="tl-body">
                    <div class="tl-head">
                      <b>{{ w.username }}</b>
                      <span class="muted">{{ w.role }}</span>
                      <span class="tag">{{ w.action }}</span>
                      <span class="muted time">{{ w.created_at }}</span>
                    </div>
                    <div class="tl-content">{{ w.content }}</div>
                  </div>
                </div>
              </div>

              <div v-if="has('worklog.write')" class="log-form">
                <el-input v-model="logText" type="textarea" :rows="2"
                          placeholder="更新工作日志，例如：已联系值班同学确认 /data 可清理" />
                <div v-if="!canHandle" class="form-row action-request">
                  <el-input v-model="requestAction" placeholder="申请处理动作，如：清理 /data 历史日志（可留空）"
                            style="max-width: 380px" />
                </div>
                <div class="form-row">
                  <el-button size="small" type="primary" :loading="busy" @click="appendLog(false)">更新工作日志</el-button>
                  <el-button v-if="!canHandle" size="small" :loading="busy" @click="appendLog(true)">提交处理请求</el-button>
                  <span class="muted tip-inline">更新会同步写入操作台账与系统日志</span>
                </div>
              </div>
            </div>

            <!-- 工程师接单执行 -->
            <div v-if="canExecute" class="soft-card">
              <div class="section-title">执行处置命令（二次确认）</div>
              <div class="form-row">
                <el-input v-model="execCommand" placeholder="如：清理 /data 历史日志"
                          style="max-width: 360px" />
                <el-button size="small" type="primary" :loading="busy" @click="execute(false)">执行</el-button>
              </div>
              <div v-if="confirmToken" class="confirm">
                该操作将修改服务器状态，请确认后继续
                <el-button size="small" type="primary" :loading="busy" @click="execute(true)">确认执行</el-button>
              </div>
              <div v-if="execResult" class="result-json">{{ execResult.stdout }} · exit_code={{ execResult.exit_code }}</div>
            </div>

            <!-- 双留痕证据 -->
            <div class="soft-card">
              <div class="section-title">系统日志留痕（{{ detail.related_audits.length }}）</div>
              <div v-for="a in detail.related_audits" :key="a.id" class="audit-row">
                <span class="tag">{{ a.action }}</span>
                <span>{{ a.username }}</span>
                <span class="muted">{{ a.result }}</span>
                <span class="muted time">{{ a.created_at }}</span>
              </div>
              <p v-if="!detail.related_audits.length" class="muted">暂无关联日志</p>
            </div>
          </template>
        </div>
      </div>

      <!-- 全局动态 -->
      <div v-else class="soft-card activity">
        <div class="section-title">四端实时操作动态（每 8 秒自动刷新）</div>
        <div v-for="a in activity" :key="a.key" class="act-row">
          <div class="act-meta">
            <span class="act-time muted truncate" :title="a.created_at">{{ a.created_at }}</span>
            <span class="act-user truncate" :title="a.username">{{ a.username }}</span>
            <span class="tag truncate" :class="{ 'tag-soft': a.kind === 'worklog' }"
                  :title="a.action">{{ a.action }}</span>
          </div>
          <span class="act-content" :title="a.content">{{ a.content }}</span>
          <button v-if="a.record_id" class="link" :title="`打开工单 ${a.record_code}`"
                  @click="openFromActivity(a)">{{ shortCode(a.record_code) }}</button>
          <span v-else-if="a.record_code" class="muted code-plain" :title="a.record_code">
            {{ shortCode(a.record_code) }}
          </span>
        </div>
        <p v-if="!activity.length" class="muted">暂无动态</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ops { flex: 1; min-height: 0; overflow-y: auto; background: var(--c-bg); }
.ops-inner { max-width: 1120px; margin: 0 auto; padding: 22px 24px 40px; }
.head-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.head-row h2 { font-size: 18px; font-weight: 600; margin: 0 0 6px; }
.head-row p { font-size: 12px; margin: 0; }
.btn-primary { border: 1px solid var(--c-blue); background: var(--c-blue); color: #fff; border-radius: 10px; padding: 9px 16px; font-size: 13px; cursor: pointer; white-space: nowrap; }
.btn-primary:hover { opacity: .9; }
.notice { background: var(--c-blue-soft); color: var(--c-blue-deep); border-radius: 10px; padding: 9px 14px; font-size: 12px; margin: 0 0 14px; }
.soft-card { background: var(--c-white); border: 1px solid var(--c-border); border-radius: 14px; padding: 18px; margin-bottom: 12px; }
.section-title { font-size: 13px; font-weight: 600; margin-bottom: 14px; }
.form-row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.new-panel { padding: 16px 18px; }
.tabs { display: flex; align-items: center; gap: 4px; margin: 4px 0 14px; }
.tabs button { border: none; background: transparent; padding: 8px 16px; border-radius: 9px; font-size: 13px; color: var(--c-text-2); cursor: pointer; }
.tabs button:hover { background: var(--c-white); }
.tabs button.on { background: var(--c-blue-soft); color: var(--c-blue); font-weight: 500; }
.spacer { flex: 1; }
.filter { border: 1px solid var(--c-border); border-radius: 8px; padding: 5px 9px; font-size: 12px; color: var(--c-text-2); background: var(--c-white); }
.counts { font-size: 11px; color: var(--c-muted); margin-left: 10px; }

.split { display: grid; grid-template-columns: 320px 1fr; gap: 14px; align-items: start; }
.list-col { display: flex; flex-direction: column; gap: 10px; max-height: 74vh; overflow-y: auto; }
.rec-card { background: var(--c-white); border: 1px solid var(--c-border); border-radius: 12px; padding: 14px; text-align: left; cursor: pointer; transition: all .15s ease; }
.rec-card:hover { border-color: var(--c-blue); }
.rec-card.selected { border-color: var(--c-blue); background: var(--c-blue-softer); }
.rec-top { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.code { font-size: 11px; color: var(--c-blue); font-weight: 600; letter-spacing: .3px; }
.status { border-radius: 999px; padding: 3px 9px; font-size: 10px; white-space: nowrap; }
.s-pending { background: var(--c-blue-soft); color: var(--c-blue-deep); }
.s-doing { background: #eef1f6; color: #52627f; }
.s-done { background: #f1f3f6; color: #6b7688; }
.s-closed { background: #f6f7f9; color: #98a0ad; }
.rec-title { font-size: 13px; font-weight: 500; margin: 9px 0 6px; color: var(--c-text); }
.rec-meta { display: flex; gap: 6px; font-size: 11px; color: var(--c-muted); }
.rec-last { margin-top: 9px; padding-top: 9px; border-top: 1px solid var(--c-border); font-size: 11px; color: var(--c-text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty-card { text-align: center; color: var(--c-muted); font-size: 12px; padding: 34px 18px; }
.detail-col { min-width: 0; }
.detail-title-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.detail-title-row h3 { font-size: 15px; font-weight: 600; margin: 6px 0 0; }
.kv-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 20px; margin-top: 16px; }
.kv-grid > div { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.kv-grid b { font-weight: 500; color: var(--c-text); line-height: 1.5; }
.issue { margin-top: 14px; padding: 10px 12px; background: var(--c-bg); border-radius: 9px; font-size: 12px; color: var(--c-text-2); line-height: 1.7; }
.pending-action { margin-top: 10px; padding: 9px 12px; background: var(--c-blue-softer); border: 1px solid var(--c-blue-soft); border-radius: 9px; font-size: 12px; color: var(--c-blue-deep); }
.action-bar { margin-top: 16px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.tip-inline { font-size: 11px; }

.timeline { border-left: 1px solid var(--c-border); margin-left: 6px; padding-left: 18px; }
.tl-item { position: relative; padding-bottom: 16px; }
.dot { position: absolute; left: -22px; top: 5px; width: 7px; height: 7px; border-radius: 50%; background: var(--c-blue); border: 2px solid var(--c-white); box-shadow: 0 0 0 1px var(--c-blue-soft); }
.tl-head { display: flex; align-items: center; gap: 8px; font-size: 11px; flex-wrap: wrap; }
.tl-head b { font-size: 12px; }
.tag { border: 1px solid var(--c-border); border-radius: 999px; padding: 2px 8px; font-size: 10px; color: var(--c-blue); background: var(--c-blue-softer); }
.tag-soft { color: #6b7688; background: #f4f6f9; }
.time { font-size: 10px; }
.tl-content { margin-top: 6px; font-size: 12px; color: var(--c-text-2); line-height: 1.7; }
.log-form { margin-top: 8px; border-top: 1px solid var(--c-border); padding-top: 14px; }
.action-request { margin-top: 10px; }
.log-form .form-row { margin-top: 10px; }
.confirm { margin-top: 12px; display: flex; align-items: center; gap: 12px; background: var(--c-blue-softer); border: 1px solid var(--c-blue-soft); border-radius: 10px; padding: 9px 12px; font-size: 12px; color: var(--c-blue-deep); }
.result-json { margin-top: 10px; background: var(--c-bg); border-radius: 9px; padding: 9px 12px; font-size: 11px; color: var(--c-text-2); }
.audit-row { display: flex; align-items: center; gap: 10px; padding: 7px 0; border-bottom: 1px solid var(--c-border); font-size: 11px; }
.audit-row:last-child { border-bottom: none; }
/* 所有文本都必须待在卡片里：单行省略 / 多行截断 / 长串折行 */
.truncate { min-width: 0; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.clamp-2 { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.rec-title, .issue, .tl-content, .pending-action, .result-json,
.detail-title-row h3, .kv-grid b, .audit-row span, .rec-last, .notice, .code-full {
  overflow-wrap: anywhere; word-break: break-word;
}
.rec-meta .truncate { max-width: 108px; }
.issue, .pending-action, .tl-content {
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.audit-row { min-width: 0; }
.audit-row span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.audit-row .time { flex: 0 0 auto; }
.reporters { font-weight: 600; color: var(--c-blue-deep); }

/* 实时动态：桌面单行省略，窄屏两行网格，工单号一律短码 */
.activity .act-row {
  display: flex; align-items: center; gap: 10px; padding: 9px 0;
  border-bottom: 1px solid var(--c-border); font-size: 12px; min-width: 0;
}
.act-meta { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; min-width: 0; max-width: 330px; }
.act-time { font-size: 10px; flex: 0 0 auto; max-width: 128px; }
.act-user { font-weight: 500; flex: 0 0 62px; max-width: 62px; }
.act-meta .tag { flex: 0 1 auto; max-width: 108px; }
.act-content {
  flex: 1 1 auto; min-width: 60px; color: var(--c-text-2);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.link {
  flex: 0 0 auto; max-width: 96px; border: none; background: transparent;
  color: var(--c-blue); font-size: 11px; cursor: pointer; padding: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.link:hover { text-decoration: underline; }
.code-plain { flex: 0 0 auto; max-width: 96px; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

@media (max-width: 640px) {
  .activity .act-row {
    display: grid; grid-template-columns: minmax(0, 1fr) auto;
    column-gap: 8px; row-gap: 5px; align-items: center;
  }
  .act-meta { grid-column: 1 / -1; max-width: 100%; }
  .act-time { max-width: 120px; }
  .act-content {
    grid-column: 1; grid-row: 2; min-width: 0; white-space: normal;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .link, .code-plain { grid-column: 2; grid-row: 2; justify-self: end; max-width: 100px; }
  .tabs { flex-wrap: wrap; }
  .counts { flex-basis: 100%; margin: 6px 0 0; }
  .head-row p { max-width: 100%; }
}

@media (max-width: 900px) {
  .split { grid-template-columns: 1fr; }
  .list-col { max-height: 260px; }
  .kv-grid { grid-template-columns: 1fr; }
  .ops-inner { padding: 16px 12px 30px; }
  .head-row { flex-direction: column; }
}
</style>
