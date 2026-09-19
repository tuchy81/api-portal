import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api/axios'

export const useTokenStore = defineStore('token', () => {
  // plainToken is NEVER stored here — only in component local ref
  const tokens = ref<any[]>([])
  const loading = ref(false)

  async function fetchList() {
    loading.value = true
    try {
      const { data } = await api.get('/tokens')
      tokens.value = data.items
    } finally {
      loading.value = false
    }
  }

  async function revokeToken(tokenId: string) {
    await api.delete(`/tokens/${tokenId}`)
    tokens.value = tokens.value.filter(t => t.token_id !== tokenId)
  }

  return { tokens, loading, fetchList, revokeToken }
})
