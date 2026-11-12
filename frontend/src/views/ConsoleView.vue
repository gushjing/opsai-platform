<script setup>
import { computed, onMounted, ref } from 'vue'
import api from '../api'

const props = defineProps({ user: Object })
const emit = defineEmits(['changed'])
const perms = computed(() => new Set((props.user && props.user.permissions) || []))
const has = (p) => perms.value.has(p)

// 默认落在有真实数据的「运维报表」，避免一进来就是一张空表
const tab = ref('report')
const datasources = ref([])
const workflows = ref([])
const runs = ref([])
const audits = ref([])
const report = ref(null)
const notice = ref('')
const loading = ref(true)
const showDsForm = ref(false)
const runningId = ref(null)
const busyExec = ref(false)

const dsForm = ref({ name: '', ds_type: 'prometheus', url: '' })
const execForm = ref({ server: '0931', command: 'df -h /data' })
const confirmToken = ref('')
const execResult = ref(null)
const wfResult = ref(null)

const statCards = computed(() => {
  const r = report.value
  if (!r) return []
  return [
    { k: '系统健康度', v: r.system_health + ' %' },
    { k: '用户数', v: r.total_users },
    { k: '对话数', v: r.total_conversations },
    { k: '审计事件', v: r.total_audit_events },
    { k: '命令执行', v: r.commands_executed },
  ]
})

function flash(text) {
  notice.value = text
  setTimeout(() => { notice.value = '' }, 4000)
}

async function loadAll() {
  loading.value = true
  try {
    if (has('datasource.manage')) datasources.value = await api.get('/datasources')
    if (has('workflow.manage')) {
      workflows.value = await api.get('/workflows')
      runs.value = await api.get('/workflows/runs')
    }
    if (has('audit.view')) audits.value = await api.get('/audit?limit=50')
    if (has('report.view')) report.value = await api.get('/reports/overview')
  } catch (e) {
    flash(e.response?.data?.detail || '部分数据加载失败')
  } finally { loading.value = false }
}
onMounted(loadAll)

async function createDS() {
  if (!dsForm.value.name.trim()) { flash('请填写数据源名称'); return }
  if (!dsForm.value.url.trim()) { flash('请填写数据源地址'); return }
  try {
    await api.post('/datasources', dsForm.value)
    dsForm.value = { name: '', ds_type: 'prometheus', url: '' }
    showDsForm.value = false
    datasources.value = await api.get('/datasources')
    flash('数据源已登记')
  } catch (e) { flash(e.response?.data?.detail || '新增失败') }
}

async function deleteDS(id) {
  try {
    await api.delete(`/datasources/${id}`)
    datasources.value = await api.get('/datasources')
    flash('数据源已删除')
  } catch (e) { flash(e.response?.data?.detail || '删除失败') }
}

async function requestExecute(withConfirm) {
  busyExec.value = true
  try {
    const payload = { ...execForm.value }
    if (withConfirm) payload.confirm_token = confirmToken.value
    const data = await api.post('/ops/execute', payload)
    if (data.need_confirm) {
      confirmToken.value = data.confirm_token
      execResult.value = null
      flash(data.warning)
    } else {
      confirmToken.value = ''
      execResult.value = data
      emit('changed')
      flash(`命令已执行，已生成工单 ${data.operation_code || ''} 并同步到工作台与日志`)
    }
  } catch (e) { flash(e.response?.data?.detail || '执行失败') }
  finally { busyExec.value = false }
}

function cancelConfirm() {
  confirmToken.value = ''
  flash('已取消本次执行，未改动服务器')
}

async function runWorkflow(id) {
  runningId.value = id
  try {
    wfResult.value = await api.post(`/workflows/${id}/run`, { context: { server: '0931' } })
    runs.value = await api.get('/workflows/runs')
    flash(`工作流执行${wfResult.value.status === 'succeeded' ? '成功' : '失败'}`)
  } catch (e) { flash(e.response?.data?.detail || '执行失败') }
  finally { runningId.value = null }
}

async function exportReport() {
  try {
    const raw = await api.get('/reports/export', { responseType: 'blob' })
    const blob = raw instanceof Blob ? raw : new Blob([JSON.stringify(raw, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `opsai_report_${new Date().toISOString().slice(0, 10)}.json`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 1500)
    flash(`报表已导出（${blob.size} 字节）`)
  } catch (e) { flash(e.response?.data?.detail || '导出失败') }
}
</script>

<template>
  <div class="console">
    <div class="console-inner">
      <p v-if="notice" class="notice truncate">{{ notice }}</p>

      <el-tabs v-model="tab">
        <!-- 默认视图：全部是真实统计值 -->
        <el-tab-pane v-if="has('report.view')" label="运维报表" name="report">
          <div v-if="statCards.length" class="cards">
            <div v-for="c in statCards" :key="c.k" class="soft-card stat">
              <div class="muted">{{ c.k }}</div>
              <div class="stat-value">{{ c.v }}</div>
            </div>
          </div>
          <div v-else class="soft-card empty-state">
            <div class="empty-title">{{ loading ? '正在读取统计数据…' : '暂无统计数据' }}</div>
          </div>
          <div class="soft-card panel">
            <div class="section-title">故障统计（{{ report?.incidents?.length || 0 }} 条）</div>
            <div v-if="report?.incidents?.length" class="table-wrap">
              <el-table :data="report.incidents" size="small" class="grid">
                <el-table-column prop="server" label="服务器" min-width="90" show-overflow-tooltip />
                <el-table-column prop="level" label="等级" min-width="80" show-overflow-tooltip />
                <el-table-column prop="problem" label="问题" min-width="180" show-overflow-tooltip />
                <el-table-column prop="status" label="状态" min-width="90" show-overflow-tooltip />
              </el-table>
            </div>
            <div v-else class="empty-inline">当前没有未恢复的故障记录</div>
            <el-button v-if="has('report.export')" class="export" @click="exportReport">导出报表</el-button>
          </div>
        </el-tab-pane>

        <el-tab-pane v-if="has('audit.view')" label="操作审计" name="audit">
          <div class="soft-card panel">
            <div class="section-title">所有敏感操作留痕（最近 {{ audits.length }} 条）</div>
            <div v-if="audits.length" class="table-wrap">
              <el-table :data="audits" size="small" class="grid grid-wide">
                <el-table-column prop="id" label="ID" min-width="60" />
                <el-table-column prop="username" label="操作人" min-width="100" show-overflow-tooltip />
                <el-table-column prop="action" label="动作" min-width="150" show-overflow-tooltip />
                <el-table-column prop="target" label="目标" min-width="140" show-overflow-tooltip />
                <el-table-column prop="result" label="结果" min-width="80" show-overflow-tooltip />
                <el-table-column prop="created_at" label="时间" min-width="150" show-overflow-tooltip />
              </el-table>
            </div>
            <div v-else class="empty-state">
              <div class="empty-title">暂无审计记录</div>
              <p>在对话、工作台或本控制台执行任一操作后，系统会在这里自动留痕。</p>
            </div>
          </div>
        </el-tab-pane>

        <el-tab-pane v-if="has('workflow.manage')" label="工作流" name="workflow">
          <div class="soft-card panel">
            <div class="section-title">流程定义（{{ workflows.length }} 条）</div>
            <div v-if="workflows.length" class="table-wrap">
              <el-table :data="workflows" size="small" class="grid">
                <el-table-column prop="name" label="名称" min-width="160" show-overflow-tooltip />
                <el-table-column label="节点数" min-width="90">
                  <template #default="scope">{{ scope.row.definition?.nodes?.length || 0 }}</template>
                </el-table-column>
                <el-table-column label="操作" min-width="100">
                  <template #default="scope">
                    <el-button link type="primary" :loading="runningId === scope.row.id"
                               @click="runWorkflow(scope.row.id)">执行</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            <div v-else class="empty-state">
              <div class="empty-title">暂无工作流定义</div>
              <p>系统内置流程会在服务启动时自动注册，若此处为空请重启后端服务。</p>
            </div>
          </div>
          <div v-if="wfResult" class="soft-card panel">
            <div class="section-title">
              最近一次执行：{{ wfResult.status === 'succeeded' ? '成功' : '失败' }} · {{ wfResult.workflow }}
            </div>
            <div class="wf-nodes">
              <div v-for="(n, i) in wfResult.log" :key="i" class="wf-node">
                <span class="tag truncate">{{ n.node }}</span>
                <span class="muted truncate">{{ n.status }}</span>
              </div>
            </div>
          </div>
          <div class="soft-card panel">
            <div class="section-title">执行记录（{{ runs.length }} 条）</div>
            <div v-if="runs.length" class="table-wrap">
              <el-table :data="runs" size="small" class="grid">
                <el-table-column prop="status" label="状态" min-width="100" show-overflow-tooltip />
                <el-table-column prop="current_node" label="结束节点" min-width="110" show-overflow-tooltip />
                <el-table-column prop="started_at" label="开始时间" min-width="160" show-overflow-tooltip />
              </el-table>
            </div>
            <div v-else class="empty-inline">还没有执行过工作流，点击上方「执行」即可生成记录</div>
          </div>
        </el-tab-pane>

        <el-tab-pane v-if="has('ops.execute')" label="服务器操控" name="ops">
          <div class="soft-card panel">
            <div class="section-title">命令执行（需二次确认）</div>
            <div class="form-row">
              <el-input v-model="execForm.server" placeholder="服务器" style="max-width: 160px" />
              <el-input v-model="execForm.command" placeholder="命令" style="max-width: 320px" />
              <el-button type="primary" :loading="busyExec" @click="requestExecute(false)">执行</el-button>
            </div>
            <div v-if="confirmToken" class="confirm">
              <span>该操作将修改服务器状态，请确认后继续（5 分钟内有效）</span>
              <el-button size="small" type="primary" :loading="busyExec" @click="requestExecute(true)">确认执行</el-button>
              <el-button size="small" @click="cancelConfirm">取消</el-button>
            </div>
            <div v-if="execResult" class="exec-card">
              <div class="exec-head">
                <b>{{ execResult.host }}</b> · exit_code={{ execResult.exit_code }} · {{ execResult.via }}
              </div>
              <pre class="exec-out">$ {{ execResult.command }}
{{ execResult.stdout }}</pre>
              <p class="muted">已生成工单 {{ execResult.operation_code }}，工作台 / 工作日志 / 系统审计四端可见</p>
            </div>
            <p class="muted hint">真实环境由 Ansible over SSH 执行；本地为模拟执行，操作全程写入审计日志。</p>
          </div>
        </el-tab-pane>

        <el-tab-pane v-if="has('datasource.manage')" label="数据源" name="datasource">
          <div class="soft-card panel">
            <div class="section-title">已登记数据源（{{ datasources.length }} 个）</div>
            <div v-if="datasources.length" class="table-wrap">
              <el-table :data="datasources" size="small" class="grid grid-mid">
                <el-table-column prop="name" label="名称" min-width="150" show-overflow-tooltip />
                <el-table-column prop="ds_type" label="类型" min-width="120" show-overflow-tooltip />
                <el-table-column prop="url" label="地址" min-width="200" show-overflow-tooltip />
                <el-table-column label="操作" min-width="90">
                  <template #default="scope">
                    <el-popconfirm title="确认删除该数据源？" confirm-button-text="删除"
                                   cancel-button-text="取消" @confirm="deleteDS(scope.row.id)">
                      <template #reference>
                        <el-button link type="primary">删除</el-button>
                      </template>
                    </el-popconfirm>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            <div v-else class="empty-state">
              <div class="empty-title">当前未登记外部数据源</div>
              <p>系统正在使用内置模拟数据（0931 / 0932 的指标、日志与链路），排障功能可正常使用。接入真实 Prometheus / ELK / 链路追踪后，可在此登记连接。</p>
            </div>

            <div v-if="showDsForm" class="ds-form">
              <div class="form-row">
                <el-input v-model="dsForm.name" placeholder="名称，如 生产 Prometheus" style="max-width: 220px" />
                <el-select v-model="dsForm.ds_type" style="width: 150px">
                  <el-option label="Prometheus" value="prometheus" />
                  <el-option label="ELK / Elasticsearch" value="elk" />
                  <el-option label="链路追踪" value="trace" />
                </el-select>
                <el-input v-model="dsForm.url" placeholder="http://127.0.0.1:9090" style="max-width: 240px" />
                <el-button type="primary" @click="createDS">提交登记</el-button>
                <el-button @click="showDsForm = false">取消</el-button>
              </div>
              <p class="muted hint">把 .env 中 USE_MOCK_DATASOURCE 改为 false 后，Agent 会改走此处登记的真实数据源。</p>
            </div>
            <div v-else class="row-actions">
              <el-button @click="showDsForm = true">＋ 登记数据源</el-button>
              <span class="muted">登记后无需重启，删除需二次确认</span>
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<style scoped>
.console { flex: 1; min-height: 0; overflow-y: auto; background: var(--c-bg); }
.console-inner { max-width: 960px; margin: 0 auto; padding: 22px 24px 40px; }
.notice { background: var(--c-blue-soft); color: var(--c-blue-deep); border-radius: 10px; padding: 9px 14px; font-size: 12px; margin: 0 0 14px; }
.truncate { min-width: 0; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.panel { padding: 20px; margin-bottom: 14px; }
.form-row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.hint { font-size: 12px; margin: 12px 0 0; line-height: 1.7; }
.confirm { margin-top: 14px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; background: var(--c-blue-softer); border: 1px solid var(--c-blue-soft); border-radius: 10px; padding: 10px 14px; font-size: 12px; color: var(--c-blue-deep); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 14px; }
.stat { padding: 16px; font-size: 12px; min-width: 0; }
.stat-value { font-size: 22px; font-weight: 600; margin-top: 8px; color: var(--c-blue); }
.export { margin-top: 16px; }
.row-actions { margin-top: 14px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.row-actions .muted { font-size: 11px; }
.ds-form { margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--c-border); }
.table-wrap { width: 100%; overflow-x: auto; }
.grid { min-width: 0; }
/* 列多的表格才允许横向滚动，列少的表在手机上自适应，避免出现空滚动条 */
.grid-wide { min-width: 640px; }
.grid-mid { min-width: 520px; }
.empty-state { text-align: left; padding: 18px 16px; background: var(--c-bg); border: 1px dashed var(--c-border); border-radius: 10px; }
.empty-title { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.empty-state p { font-size: 12px; color: var(--c-text-2); line-height: 1.7; margin: 0; }
.empty-inline { font-size: 12px; color: var(--c-muted); padding: 14px 2px; }
.exec-card { margin-top: 14px; border: 1px solid var(--c-border); border-radius: 10px; padding: 12px 14px; }
.exec-head { font-size: 12px; color: var(--c-text-2); }
.exec-out { margin: 10px 0 0; padding: 10px 12px; background: var(--c-bg); border-radius: 8px; font-size: 11px; line-height: 1.7; color: var(--c-text-2); white-space: pre-wrap; overflow-wrap: anywhere; max-height: 220px; overflow: auto; font-family: ui-monospace, Consolas, monospace; }
.exec-card p { font-size: 11px; margin: 10px 0 0; }
.wf-nodes { display: flex; flex-wrap: wrap; gap: 8px; }
.wf-node { display: flex; align-items: center; gap: 6px; border: 1px solid var(--c-border); border-radius: 999px; padding: 4px 10px; font-size: 11px; max-width: 200px; }
.wf-node .tag { max-width: 90px; }
.wf-node .muted { max-width: 70px; }
@media (max-width: 768px) {
  .console-inner { padding: 16px 12px 30px; }
  .panel { padding: 14px; }
  .console-inner :deep(.el-tabs__item) { font-size: 12px; padding: 0 10px; }
}
</style>
