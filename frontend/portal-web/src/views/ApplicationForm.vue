<template>
  <div class="card">
    <h2>API 사용 신청</h2>
    <p v-if="apiDetail" class="api-name">{{ apiDetail.name }} <code>{{ apiDetail.public_path }}</code></p>

    <form @submit.prevent="submit">
      <fieldset class="scope-set">
        <legend>요청 Scope <span class="required">*</span></legend>
        <p class="hint">필요한 권한만 선택하세요. 승인 시 서버가 카탈로그 정의와 교집합으로 부여합니다.</p>
        <label v-for="s in availableScopes" :key="s.scope_id || s.scope_name" class="scope-option">
          <input type="checkbox" :value="s.scope_name" v-model="form.requestedScopes" />
          <span class="scope-name"><code>{{ s.scope_name }}</code></span>
          <span class="scope-meta">{{ s.http_method }} {{ s.path_pattern }}</span>
          <span v-if="s.description" class="scope-desc">— {{ s.description }}</span>
        </label>
        <p v-if="!availableScopes.length && apiDetail" class="warn">이 API에 정의된 Scope가 없습니다. API 오너에게 문의하세요.</p>
      </fieldset>

      <label>신청 목적: <textarea v-model="form.purpose" class="input" rows="3" required></textarea></label>
      <label>예상 TPS: <input v-model.number="form.expectedTps" type="number" min="1" max="100" class="input" /></label>
      <label>일 예상 호출량: <input v-model.number="form.expectedDaily" type="number" min="1" class="input" /></label>
      <label>사용 기간 (종료일): <input v-model="form.validUntil" type="date" class="input" required /></label>

      <div style="margin-top:1rem">
        <button class="btn btn-primary" type="submit" :disabled="submitting || !form.requestedScopes.length">신청 제출</button>
        <router-link to="/cdp/catalog" class="btn" style="margin-left:0.5rem">취소</router-link>
        <span v-if="!form.requestedScopes.length" class="warn" style="margin-left:0.6rem">Scope를 1개 이상 선택하세요.</span>
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
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api/axios'

const route = useRoute()
const submitting = ref(false)
const result = ref<any>(null)
const apiDetail = ref<any>(null)
const form = ref({
  purpose: '',
  expectedTps: 5,
  expectedDaily: 2000,
  validUntil: '2027-03-31',
  requestedScopes: [] as string[],
})

const availableScopes = computed<any[]>(() => apiDetail.value?.scopes || [])

onMounted(async () => {
  try {
    const { data } = await api.get(`/catalog/apis/${route.params.apiId}`)
    apiDetail.value = data
  } catch {
    alert('API 정보를 불러오지 못했습니다.')
  }
})

async function submit() {
  if (!form.value.requestedScopes.length) return
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
.api-name { color:#555; margin-bottom:1rem; }
.scope-set { border:1px solid #e0e0e0; border-radius:6px; padding:1rem; margin-bottom:1.2rem; }
.scope-set legend { font-size:0.9rem; font-weight:600; padding:0 0.4rem; }
.hint { font-size:0.8rem; color:#666; margin-bottom:0.8rem; }
.scope-option { display:flex; align-items:center; gap:0.5rem; margin-bottom:0.5rem; font-size:0.85rem; }
.scope-meta { color:#666; font-size:0.78rem; }
.scope-desc { color:#888; font-size:0.78rem; }
.required { color:#c62828; }
.warn { color:#e65100; font-size:0.8rem; }
</style>
