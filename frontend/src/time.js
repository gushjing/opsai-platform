import api from './api'

// 以服务器（即本机）本地时间为准，校准浏览器时钟，保证所有显示时间与本地时间一致。
let offsetMs = 0
let tzOffsetMinutes = 480
let synced = false

function parseOffset(text) {
  const m = /^([+-])(\d{2})(\d{2})$/.exec(text || '')
  if (!m) return 480
  const n = Number(m[2]) * 60 + Number(m[3])
  return m[1] === '-' ? -n : n
}

export async function syncClock() {
  try {
    const meta = await api.get('/meta')
    if (meta && meta.server_now_iso) {
      offsetMs = new Date(meta.server_now_iso).getTime() - Date.now()
      tzOffsetMinutes = parseOffset(meta.utc_offset)
      synced = true
    }
    return meta
  } catch {
    return null
  }
}

function pad(n) { return String(n).padStart(2, '0') }

export function formatClock() {
  // 用 UTC getter + 明确的时区偏移，避免访客浏览器时区不同导致显示错误
  const d = new Date(Date.now() + offsetMs + tzOffsetMinutes * 60000)
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
         `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`
}

export function clockSynced() { return synced }
