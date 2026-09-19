<template>
  <div>
    <h2>내 신청 현황</h2>
    <div class="card">
      <table>
        <thead><tr><th>신청 ID</th><th>API 이름</th><th>상태</th><th>신청일</th><th>부여 Scope</th></tr></thead>
        <tbody>
          <tr v-for="app in apps" :key="app.app_id">
            <td><code>{{ app.app_id?.slice(0,8) }}...</code></td>
            <td>{{ app.api_name }}</td>
            <td><span :class="`badge badge-${app.status?.toLowerCase()}`">{{ app.status }}</span></td>
            <td>{{ app.created_at?.slice(0,10) }}</td>
            <td>{{ (app.granted_scopes || []).join(', ') || '-' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!apps.length">신청 내역이 없습니다.</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import api from '../api/axios'

const apps = ref<any[]>([])
onMounted(async () => {
  const { data } = await api.get('/applications')
  apps.value = data.items
})
</script>
