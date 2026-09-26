<template>
  <div>
    <h2>감사 로그</h2>
    <el-card shadow="never">
      <el-table :data="logs" style="width: 100%">
        <el-table-column label="시각" width="170">
          <template #default="{ row }">{{ row.occurred_at?.slice(0, 19) }}</template>
        </el-table-column>
        <el-table-column prop="event_type" label="이벤트" width="140" />
        <el-table-column label="Token ID" width="120">
          <template #default="{ row }"><code>{{ row.token_id || '-' }}</code></template>
        </el-table-column>
        <el-table-column label="사용자" width="120">
          <template #default="{ row }">{{ row.user_sub || '-' }}</template>
        </el-table-column>
        <el-table-column label="경로">
          <template #default="{ row }"><code>{{ row.request_path || '-' }}</code></template>
        </el-table-column>
        <el-table-column label="상태코드" width="90">
          <template #default="{ row }">{{ row.status_code || '-' }}</template>
        </el-table-column>
        <el-table-column label="오류코드" width="100">
          <template #default="{ row }">{{ row.error_code || '-' }}</template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!logs.length" description="감사 로그가 없습니다." />
      <el-button class="load-more" @click="loadMore">더 보기</el-button>
    </el-card>
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

<style scoped>
.load-more { margin-top: 1rem; }
</style>
