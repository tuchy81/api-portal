<template>
  <div>
    <h2>PAT 관리</h2>
    <button class="btn btn-primary" style="margin-bottom:1rem" @click="showIssueDialog = true">새 PAT 발급</button>

    <!-- PAT 발급 다이얼로그 (spec 7.4 보안 UX 7개 요건) -->
    <div v-if="showIssueDialog" class="modal-overlay">
      <div class="modal">
        <h3>PAT 발급</h3>
        <div v-if="!plainToken">
          <label>신청 ID (appId): <input v-model="form.appId" class="input" placeholder="승인된 신청 app_id" /></label>
          <label>토큰 이름: <input v-model="form.tokenName" class="input" /></label>
          <label>유효기간 (일, 1~90): <input v-model.number="form.validDays" type="number" min="1" max="90" class="input" /></label>
          <p class="warning">⚠️ 발급된 PAT은 이 화면에서만 1회 확인 가능합니다. 안전한 곳에 보관하세요.</p>
          <button class="btn btn-primary" @click="issueToken" :disabled="issuing">발급</button>
          <button class="btn" @click="closeDialog" style="margin-left:0.5rem">취소</button>
        </div>
        <div v-else class="token-display">
          <p class="warning-banner">🔐 이 창을 닫으면 다시 확인할 수 없습니다!</p>
          <div class="token-box">
            <code>{{ plainToken }}</code>
          </div>
          <button class="btn btn-primary" @click="copyToken">{{ copied ? '복사됨 ✓' : '클립보드 복사' }}</button>
          <label class="confirm-label">
            <input type="checkbox" v-model="confirmed" />
            토큰을 안전한 곳에 보관했습니다. (.env 파일 권장, 소스코드 하드코딩 금지)
          </label>
          <button class="btn btn-danger" @click="closeDialog" :disabled="!confirmed">닫기</button>
        </div>
      </div>
    </div>

    <!-- PAT 목록 -->
    <div class="card">
      <table>
        <thead>
          <tr><th>Token ID</th><th>이름</th><th>Scope</th><th>상태</th><th>만료일</th><th>작업</th></tr>
        </thead>
        <tbody>
          <tr v-for="t in tokenStore.tokens" :key="t.token_id">
            <td><code>{{ t.token_id }}</code></td>
            <td>{{ t.token_name }}</td>
            <td>{{ (t.scopes || []).join(', ') }}</td>
            <td><span :class="`badge badge-${t.status?.toLowerCase()}`">{{ t.status }}</span></td>
            <td>{{ t.expires_at?.slice(0,10) }}</td>
            <td>
              <button v-if="t.status === 'ACTIVE'" class="btn btn-danger" @click="revoke(t.token_id)">폐기</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!tokenStore.tokens.length && !tokenStore.loading">발급된 PAT이 없습니다.</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useTokenStore } from '../stores/token'
import api from '../api/axios'

const tokenStore = useTokenStore()
const showIssueDialog = ref(false)
const plainToken = ref<string | null>(null)  // 로컬 전용, 스토어 저장 금지
const copied = ref(false)
const confirmed = ref(false)
const issuing = ref(false)

const form = ref({ appId: '', tokenName: '자동화 스크립트', validDays: 90 })

onMounted(() => tokenStore.fetchList())

onUnmounted(() => {
  plainToken.value = null  // 명시적 소거
})

async function issueToken() {
  issuing.value = true
  try {
    const { data } = await api.post('/tokens', form.value)
    plainToken.value = data.token  // 화면 표시용 1회 보관
    await tokenStore.fetchList()
  } catch (e: any) {
    alert(e.response?.data?.detail || '발급 실패')
  } finally {
    issuing.value = false
  }
}

async function copyToken() {
  if (plainToken.value) {
    try {
      await navigator.clipboard.writeText(plainToken.value)
      copied.value = true
    } catch {
      alert('수동으로 복사하세요: ' + plainToken.value)
    }
  }
}

function closeDialog() {
  if (plainToken.value && !confirmed.value) {
    if (!confirm('정말 닫겠습니까? 토큰을 다시 확인할 수 없습니다.')) return
  }
  plainToken.value = null  // 소거
  showIssueDialog.value = false
  confirmed.value = false
  copied.value = false
  form.value = { appId: '', tokenName: '자동화 스크립트', validDays: 90 }
}

async function revoke(tokenId: string) {
  if (!confirm(`PAT ${tokenId}을 폐기하시겠습니까?`)) return
  await tokenStore.revokeToken(tokenId)
}
</script>

<style scoped>
h2 { margin-bottom: 1rem; }
.modal-overlay { position: fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); display:flex; justify-content:center; align-items:center; z-index:1000; }
.modal { background:white; border-radius:8px; padding:2rem; max-width:600px; width:90%; }
.modal h3 { margin-bottom:1.5rem; }
label { display:block; margin-bottom:0.8rem; }
.input { width:100%; padding:0.4rem 0.8rem; border:1px solid #ccc; border-radius:4px; margin-top:0.2rem; }
.warning { color:#e65100; font-size:0.85rem; margin:1rem 0; }
.warning-banner { background:#fff3e0; border:1px solid #fb8c00; padding:0.8rem; border-radius:4px; margin-bottom:1rem; font-weight:bold; }
.token-box { background:#1a1a2e; border-radius:4px; padding:1rem; margin-bottom:1rem; word-break:break-all; }
.token-box code { color:#4fc3f7; font-size:0.85rem; }
.confirm-label { display:flex; align-items:center; gap:0.5rem; margin:1rem 0; font-size:0.85rem; }
</style>
