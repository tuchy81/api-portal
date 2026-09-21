<template>
  <div>
    <h2>결재함 (API 오너)</h2>
    <p class="notice">
      부여 Scope는 서버가 <strong>신청 Scope ∩ 카탈로그 정의</strong>로 계산합니다. 결재자가 임의로 수정할 수 없습니다.
    </p>
    <div class="card">
      <table>
        <thead>
          <tr>
            <th>신청 ID</th><th>API</th><th>신청자</th><th>목적</th>
            <th>신청 Scope</th><th>상태</th><th>작업</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="app in apps" :key="app.app_id">
            <td><code>{{ app.app_id?.slice(0,8) }}...</code></td>
            <td>{{ app.api_name }}</td>
            <td>{{ app.user_sub }}</td>
            <td>{{ app.purpose?.slice(0,40) }}...</td>
            <td>
              <span v-for="s in (app.requested_scopes || [])" :key="s" class="scope-chip">{{ s }}</span>
              <span v-if="!(app.requested_scopes || []).length" class="muted">-</span>
            </td>
            <td><span :class="`badge badge-${app.status?.toLowerCase()}`">{{ app.status }}</span></td>
            <td v-if="app.status === 'PENDING'">
              <button class="btn btn-success" @click="approve(app)" :disabled="busy">승인</button>
              <button class="btn btn-danger" style="margin-left:0.3rem" @click="reject(app)" :disabled="busy">반려</button>
            </td>
            <td v-else>
              <span v-for="s in (app.granted_scopes || [])" :key="s" class="scope-chip granted">{{ s }}</span>
              <span v-if="!(app.granted_scopes || []).length" class="muted">-</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!apps.length" class="muted">결재 대기 중인 신청이 없습니다.</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import api from '../api/axios'

const apps = ref<any[]>([])
const busy = ref(false)

async function reload() {
  const { data } = await api.get('/applications', { params: { role: 'reviewer' } })
  apps.value = data.items || []
}

onMounted(reload)

async function approve(app: any) {
  const scopes = (app.requested_scopes || []).join(', ') || '(없음)'
  if (!confirm(`신청 ${app.app_id?.slice(0,8)}을 승인하시겠습니까?\n\n신청 Scope: ${scopes}\n실제 부여 Scope는 카탈로그 정의와 교집합으로 서버가 결정합니다.`)) return
  busy.value = true
  try {
    await api.patch(`/applications/${app.app_id}`, { action: 'APPROVE' })
    await reload()
  } catch (e: any) {
    alert(e.response?.data?.detail || '승인 실패')
  } finally {
    busy.value = false
  }
}

async function reject(app: any) {
  const comment = prompt('반려 사유:')
  if (comment === null) return
  busy.value = true
  try {
    await api.patch(`/applications/${app.app_id}`, { action: 'REJECT', reviewComment: comment })
    await reload()
  } catch (e: any) {
    alert(e.response?.data?.detail || '반려 실패')
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
h2 { margin-bottom:0.8rem; }
.notice { font-size:0.85rem; color:#1565c0; background:#e3f2fd; border:1px solid #90caf9; border-radius:6px; padding:0.6rem 0.9rem; margin-bottom:1rem; }
.scope-chip { display:inline-block; background:#eceff1; border-radius:10px; padding:0.15rem 0.55rem; font-size:0.75rem; margin:0 0.2rem 0.2rem 0; font-family:monospace; }
.scope-chip.granted { background:#e8f5e9; color:#2e7d32; }
.muted { color:#888; font-size:0.85rem; }
</style>
