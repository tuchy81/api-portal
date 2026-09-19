<template>
  <div>
    <h2>결재함 (API 오너)</h2>
    <div class="card">
      <table>
        <thead><tr><th>신청 ID</th><th>API</th><th>신청자</th><th>목적</th><th>상태</th><th>작업</th></tr></thead>
        <tbody>
          <tr v-for="app in apps" :key="app.app_id">
            <td><code>{{ app.app_id?.slice(0,8) }}...</code></td>
            <td>{{ app.api_name }}</td>
            <td>{{ app.user_sub }}</td>
            <td>{{ app.purpose?.slice(0,40) }}...</td>
            <td><span :class="`badge badge-${app.status?.toLowerCase()}`">{{ app.status }}</span></td>
            <td v-if="app.status === 'PENDING'">
              <button class="btn btn-success" @click="approve(app)">승인</button>
              <button class="btn btn-danger" style="margin-left:0.3rem" @click="reject(app)">반려</button>
            </td>
            <td v-else>-</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import api from '../api/axios'

const apps = ref<any[]>([])

onMounted(async () => {
  const { data } = await api.get('/applications', { params: { role: 'reviewer' } })
  apps.value = data.items
})

async function approve(app: any) {
  const scopes = prompt('부여할 Scope (쉼표 구분):', app.api_code === 'MDM-VENDOR' ? 'capi.vendor.read' : 'capi.order.read')
  if (!scopes) return
  await api.patch(`/applications/${app.app_id}`, {
    action: 'APPROVE',
    grantedScopes: scopes.split(',').map((s: string) => s.trim()),
  })
  const { data } = await api.get('/applications', { params: { role: 'reviewer' } })
  apps.value = data.items
}

async function reject(app: any) {
  const comment = prompt('반려 사유:')
  await api.patch(`/applications/${app.app_id}`, { action: 'REJECT', reviewComment: comment })
  const { data } = await api.get('/applications', { params: { role: 'reviewer' } })
  apps.value = data.items
}
</script>
