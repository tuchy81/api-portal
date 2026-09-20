import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api/axios'

export const useAuthStore = defineStore('auth', () => {
  const sub = ref('')
  const username = ref('')
  const roles = ref<string[]>([])

  const isAdmin = computed(() => roles.value.includes('platform-admin'))
  const isApiOwner = computed(() => roles.value.includes('api-owner'))
  const isAuditor = computed(() => roles.value.includes('auditor'))
  const isAuthenticated = computed(() => !!sub.value)

  async function fetchMe() {
    try {
      const { data } = await api.get('/me')
      sub.value = data.sub
      username.value = data.username
      roles.value = data.roles
    } catch {
      console.warn('Not authenticated')
    }
  }

  async function setToken(token: string) {
    localStorage.setItem('cdp_auth_token', token)
    await fetchMe()
  }

  function logout() {
    localStorage.removeItem('cdp_auth_token')
    sub.value = ''
    username.value = ''
    roles.value = []
    window.location.hash = '/login'
  }

  return { sub, username, roles, isAdmin, isApiOwner, isAuditor, isAuthenticated, fetchMe, setToken, logout }
})
