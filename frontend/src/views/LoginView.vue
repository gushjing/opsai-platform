<script setup>
import { ref } from 'vue'
import api from '../api'

const emit = defineEmits(['login'])
const username = ref('admin')
const DEMO_PW = import.meta.env.VITE_DEMO_PASSWORD || 'OpsAI@Demo2026'
const password = ref(DEMO_PW)
const loading = ref(false)
const error = ref('')

const accounts = [
  { label: '管理员', username: 'admin', password: DEMO_PW },
  { label: '工程师', username: 'engineer', password: DEMO_PW },
  { label: '普通用户', username: 'user', password: DEMO_PW },
  { label: '领导', username: 'leader', password: DEMO_PW },
]

function fill(a) {
  username.value = a.username
  password.value = a.password
  error.value = ''
}

async function submit() {
  if (!username.value || !password.value) return
  loading.value = true
  error.value = ''
  try {
    const data = await api.post('/auth/login', {
      username: username.value,
      password: password.value,
    })
    localStorage.setItem('opsai_token', data.access_token)
    localStorage.setItem('opsai_user', JSON.stringify(data.user))
    emit('login', data.user)
  } catch (e) {
    error.value = e.response?.data?.detail || '登录失败，请检查服务是否已启动'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="brand">
        <span class="logo">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
            <path d="M4 15c3-7 8-9 16-8-1 8-5 12-11 11-2 0-4-1-5-3z" fill="#fff" opacity=".95"/>
            <path d="M7 16c3-3 6-4 9-4" stroke="#4c6fff" stroke-width="1.6" stroke-linecap="round"/>
          </svg>
        </span>
        <span>智能排障平台</span>
      </div>
      <p class="subtitle">用一句话定位服务器故障</p>
      <p class="intro">接入指标、日志与链路数据，自动识别问题并定位故障位置。</p>

      <div class="field">
        <label>账号</label>
        <el-input v-model="username" size="large" placeholder="请输入账号" @keyup.enter="submit" />
      </div>
      <div class="field">
        <label>密码</label>
        <el-input v-model="password" size="large" type="password" placeholder="请输入密码"
                  show-password @keyup.enter="submit" />
      </div>
      <p v-if="error" class="error">{{ error }}</p>
      <el-button type="primary" size="large" class="submit" :loading="loading" @click="submit">
        进入工作台
      </el-button>

      <div class="divider"></div>
      <p class="section-title">演示账号（点击自动填充）</p>
      <div class="accounts">
        <button v-for="a in accounts" :key="a.username" class="pill" @click="fill(a)">{{ a.label }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: var(--c-white); padding: 24px;
}
.login-card { width: 100%; max-width: 400px; border: 1px solid var(--c-border); border-radius: 20px; padding: 36px 34px 30px; }
.brand { display: flex; align-items: center; gap: 10px; font-size: 19px; font-weight: 600; }
.logo { width: 34px; height: 34px; border-radius: 11px; background: var(--c-blue); display: grid; place-items: center; }
.subtitle { margin: 22px 0 4px; font-size: 15px; font-weight: 500; }
.intro { margin: 0 0 28px; font-size: 13px; color: var(--c-muted); line-height: 1.7; }
.field { margin-bottom: 16px; }
.field label { display: block; margin-bottom: 7px; font-size: 12px; color: var(--c-text-2); }
.error { color: #d94b4b; font-size: 12px; margin: 0 0 10px; }
.submit { width: 100%; border-radius: 11px; margin-top: 6px; }
.divider { margin: 26px 0 18px; }
.accounts { display: flex; flex-wrap: wrap; gap: 8px; }
.accounts .pill { padding: 6px 13px; font-size: 12px; }
</style>
