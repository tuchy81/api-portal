<template>
  <div class="card">
    <h2>API 사용 신청</h2>
    <form @submit.prevent="submit">
      <label>신청 목적: <textarea v-model="form.purpose" class="input" rows="3" required></textarea></label>
      <label>예상 TPS: <input v-model.number="form.expectedTps" type="number" min="1" max="100" class="input" /></label>
      <label>일 예상 호출량: <input v-model.number="form.expectedDaily" type="number" min="1" class="input" /></label>
      <label>사용 기간 (종료일): <input v-model="form.validUntil" type="date" class="input" required /></label>
      <div style="margin-top:1rem">
        <button class="btn btn-primary" type="submit" :disabled="submitting">신청 제출</button>
        <router-link to="/cdp/catalog" class="btn" style="margin-left:0.5rem">취소</router-link>
      </div>
    </form>
    <div v-if="result" class="result-box">
      <p>✅ 신청 완료! 신청 ID: <strong>{{ result.appId }}</strong></p>
      <p>상태: {{ result.status }}</p>
      <p v-if="result.eligibility?.missingRoles?.length">⚠️ 누락 권한: {{ result.eligibility.missingRoles.join(', ') }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api/axios'

const route = useRoute()
const submitting = ref(false)
const result = ref<any>(null)
const form = ref({ purpose: '', expectedTps: 5, expectedDaily: 2000, validUntil: '2027-03-31', requestedScopes: [] })

onMounted(() => {
  // Pre-fill apiId from route
})

async function submit() {
  submitting.value = true
  try {
    const { data } = await api.post('/applications', {
      apiId: route.params.apiId,
      ...form.value,
    })
    result.value = data
  } catch (e: any) {
    alert(e.response?.data?.detail || '신청 실패')
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
label { display:block; margin-bottom:0.8rem; }
.input { width:100%; padding:0.4rem 0.8rem; border:1px solid #ccc; border-radius:4px; margin-top:0.2rem; }
.result-box { background:#e8f5e9; padding:1rem; border-radius:6px; margin-top:1rem; }
</style>
