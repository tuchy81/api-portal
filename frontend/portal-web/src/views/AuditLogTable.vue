<template>
  <div>
    <h2>감사 로그</h2>
    <div class="card">
      <table>
        <thead><tr><th>시각</th><th>이벤트</th><th>Token ID</th><th>사용자</th><th>경로</th><th>상태코드</th><th>오류코드</th></tr></thead>
        <tbody>
          <tr v-for="log in logs" :key="log.log_id">
            <td>{{ log.occurred_at?.slice(0,19) }}</td>
            <td>{{ log.event_type }}</td>
            <td><code>{{ log.token_id || '-' }}</code></td>
            <td>{{ log.user_sub || '-' }}</td>
            <td><code>{{ log.request_path || '-' }}</code></td>
            <td>{{ log.status_code || '-' }}</td>
            <td>{{ log.error_code || '-' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!logs.length">감사 로그가 없습니다.</p>
      <button class="btn btn-primary" style="margin-top:1rem" @click="loadMore">더 보기</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import api from '../api/axios'

const logs = ref<any[]>([])
const page = ref(1)

async function fetchLogs() {
  const { data } = await api.get('/audit/logs', { params: { page: page.value } })
  logs.value = [...logs.value, ...data.items]
}

async function loadMore() {
  page.value++
  await fetchLogs()
}

onMounted(fetchLogs)
</script>
