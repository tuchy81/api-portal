<template>
  <div class="login-page">
    <el-card class="login-card" shadow="always">
      <div class="brand">🏢 시민개발자 API 포털</div>
      <h2>개발 환경 로그인</h2>
      <el-alert type="warning" :closable="false" show-icon class="notice">
        Mock 모드 — 실제 Keycloak 없이 시뮬레이션합니다.
      </el-alert>

      <el-radio-group v-model="selectedId" class="user-list">
        <el-radio
          v-for="u in users"
          :key="u.id"
          :value="u.id"
          border
          class="user-item"
        >
          <div class="user-info">
            <span class="username">{{ u.username }}</span>
            <span class="roles">{{ u.roles.join(', ') }}</span>
          </div>
        </el-radio>
      </el-radio-group>

      <el-alert v-if="error" type="error" :closable="false" show-icon class="error">{{ error }}</el-alert>

      <el-button type="primary" size="large" class="submit-btn" :disabled="!selectedId" :loading="loading" @click="login">
        로그인
      </el-button>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import api from '../api/axios'

interface DevUser { id: string; username: string; roles: string[] }

const router = useRouter()
const auth = useAuthStore()

const users = ref<DevUser[]>([])
const selectedId = ref('')
const loading = ref(false)
const error = ref('')

onMounted(async () => {
  try {
    const { data } = await api.get('/auth/dev-users')
    users.value = data.users
    if (users.value.length) selectedId.value = users.value[0].id
  } catch {
    error.value = '사용자 목록을 불러오지 못했습니다.'
  }
})

async function login() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await api.post('/auth/dev-login', { userId: selectedId.value })
    auth.setToken(data.token)
    router.push('/cdp/catalog')
  } catch (e: any) {
    error.value = e.response?.data?.error || '로그인 실패'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f5f6fa;
}
.login-card { width: 440px; }
.brand { font-size: 1.05rem; font-weight: bold; color: #1a1a2e; margin-bottom: 0.5rem; }
h2 { margin: 0 0 1rem; font-size: 1.35rem; }
.notice { margin-bottom: 1.2rem; }
.user-list { display: flex; flex-direction: column; flex-wrap: nowrap; gap: 0.6rem; width: 100%; margin-bottom: 1.2rem; }
/* Element Plus's bordered .el-radio carries a default margin-right:30px
   (meant for horizontal layout) that only its own :last-child rule zeroes
   out — combined with .el-radio-group's align-items:center, that asymmetry
   centers every non-last card 15px left of the last one. Zero it on all of
   them so every card aligns flush at the same left edge. */
.user-item { height: auto; width: 100%; margin: 0 !important; padding: 0.7rem 1rem; }
.user-item :deep(.el-radio__label) { width: 100%; }
.user-info { display: flex; flex-direction: column; }
.username { font-weight: 600; font-size: 0.95rem; }
.roles { font-size: 0.78rem; color: #888; margin-top: 0.15rem; }
.error { margin-bottom: 1rem; }
.submit-btn { width: 100%; }
</style>
