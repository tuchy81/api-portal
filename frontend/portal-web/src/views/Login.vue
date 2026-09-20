<template>
  <div class="login-page">
    <div class="login-card">
      <div class="brand">🏢 시민개발자 API 포털</div>
      <h2>개발 환경 로그인</h2>
      <p class="notice">⚠️ Mock 모드 — 실제 Keycloak 없이 시뮬레이션합니다.</p>

      <div class="user-list">
        <label
          v-for="u in users"
          :key="u.id"
          class="user-item"
          :class="{ selected: selectedId === u.id }"
        >
          <input type="radio" v-model="selectedId" :value="u.id" />
          <div class="user-info">
            <span class="username">{{ u.username }}</span>
            <span class="roles">{{ u.roles.join(', ') }}</span>
          </div>
        </label>
      </div>

      <p v-if="error" class="error">{{ error }}</p>

      <button class="btn btn-primary" :disabled="!selectedId || loading" @click="login">
        {{ loading ? '로그인 중...' : '로그인' }}
      </button>
    </div>
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
.login-card {
  background: white;
  border-radius: 12px;
  padding: 2.5rem;
  width: 420px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.12);
}
.brand {
  font-size: 1.1rem;
  font-weight: bold;
  color: #1a1a2e;
  margin-bottom: 0.5rem;
}
h2 {
  margin-bottom: 0.5rem;
  font-size: 1.4rem;
}
.notice {
  font-size: 0.82rem;
  color: #e65100;
  background: #fff3e0;
  border: 1px solid #ffcc80;
  border-radius: 4px;
  padding: 0.5rem 0.8rem;
  margin-bottom: 1.5rem;
}
.user-list {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  margin-bottom: 1.2rem;
}
.user-item {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  border: 2px solid #e0e0e0;
  border-radius: 8px;
  padding: 0.8rem 1rem;
  cursor: pointer;
  transition: border-color 0.15s;
}
.user-item.selected {
  border-color: #1565c0;
  background: #e3f2fd;
}
.user-item input[type="radio"] {
  accent-color: #1565c0;
  width: 16px;
  height: 16px;
  flex-shrink: 0;
}
.user-info {
  display: flex;
  flex-direction: column;
}
.username {
  font-weight: 600;
  font-size: 0.95rem;
}
.roles {
  font-size: 0.78rem;
  color: #666;
  margin-top: 0.15rem;
}
.btn {
  width: 100%;
  padding: 0.7rem;
  border: none;
  border-radius: 8px;
  cursor: pointer;
  font-size: 1rem;
  font-weight: 600;
}
.btn-primary {
  background: #1565c0;
  color: white;
}
.btn-primary:disabled {
  background: #90caf9;
  cursor: not-allowed;
}
.error {
  color: #c62828;
  font-size: 0.85rem;
  margin-bottom: 0.8rem;
}
</style>
