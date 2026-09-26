<template>
  <div>
    <h2>내 신청 현황</h2>
    <el-card shadow="never">
      <el-table :data="apps" style="width: 100%">
        <el-table-column label="신청 ID" width="120">
          <template #default="{ row }"><code>{{ row.app_id?.slice(0, 8) }}...</code></template>
        </el-table-column>
        <el-table-column prop="api_name" label="API 이름" />
        <el-table-column label="상태" width="120">
          <template #default="{ row }"><el-tag :type="statusTagType(row.status)">{{ row.status }}</el-tag></template>
        </el-table-column>
        <el-table-column label="신청일" width="120">
          <template #default="{ row }">{{ row.created_at?.slice(0, 10) }}</template>
        </el-table-column>
        <el-table-column label="부여 Scope">
          <template #default="{ row }">{{ (row.granted_scopes || []).join(', ') || '-' }}</template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!apps.length" description="신청 내역이 없습니다." />
    </el-card>
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

function statusTagType(status: string) {
  const map: Record<string, string> = { APPROVED: 'success', PENDING: 'warning', REJECTED: 'danger', EXPIRED: 'info', WITHDRAWN: 'info' }
  return map[status] || 'info'
}
</script>
