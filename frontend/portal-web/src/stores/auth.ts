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
    } catch (e) {
      console.warn('Not authenticated')
    }
  }

  function setToken(token: string) {
    localStorage.setItem('cdp_auth_token', token)
    fetchMe()
  }

  return { sub, username, roles, isAdmin, isApiOwner, isAuditor, isAuthenticated, fetchMe, setToken }
})
