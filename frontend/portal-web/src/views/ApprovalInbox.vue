<template>
  <div>
    <h2>결재함 (API 오너)</h2>
    <el-alert type="info" :closable="false" show-icon class="notice">
      부여 Scope는 서버가 <strong>신청 Scope ∩ 카탈로그 정의</strong>로 계산합니다. 결재자가 임의로 수정할 수 없습니다.
    </el-alert>
    <el-card shadow="never">
      <el-table :data="apps" style="width: 100%">
        <el-table-column label="신청 ID" width="110">
          <template #default="{ row }"><code>{{ row.app_id?.slice(0, 8) }}...</code></template>
        </el-table-column>
        <el-table-column prop="api_name" label="API" />
        <el-table-column prop="user_sub" label="신청자" width="110" />
        <el-table-column label="목적">
          <template #default="{ row }">{{ row.purpose?.slice(0, 40) }}...</template>
        </el-table-column>
        <el-table-column label="신청 Scope" min-width="180">
          <template #default="{ row }">
            <el-tag v-for="s in row.requested_scopes || []" :key="s" size="small" class="scope-chip">{{ s }}</el-tag>
            <span v-if="!(row.requested_scopes || []).length" class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="상태" width="110">
          <template #default="{ row }"><el-tag :type="statusTagType(row.status)">{{ row.status }}</el-tag></template>
        </el-table-column>
        <el-table-column label="작업" width="160">
          <template #default="{ row }">
            <template v-if="row.status === 'PENDING'">
              <el-button type="success" size="small" :disabled="busy" @click="approve(row)">승인</el-button>
              <el-button type="danger" size="small" :disabled="busy" @click="reject(row)">반려</el-button>
            </template>
            <template v-else>
              <el-tag v-for="s in row.granted_scopes || []" :key="s" size="small" type="success" class="scope-chip">{{ s }}</el-tag>
              <span v-if="!(row.granted_scopes || []).length" class="muted">-</span>
            </template>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!apps.length" description="결재 대기 중인 신청이 없습니다." />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api/axios'

const apps = ref<any[]>([])
const busy = ref(false)

async function reload() {
  const { data } = await api.get('/applications', { params: { role: 'reviewer' } })
  apps.value = data.items || []
}

onMounted(reload)

function statusTagType(status: string) {
  const map: Record<string, string> = { APPROVED: 'success', PENDING: 'warning', REJECTED: 'danger', EXPIRED: 'info', WITHDRAWN: 'info' }
  return map[status] || 'info'
}

async function approve(app: any) {
  const scopes = (app.requested_scopes || []).join(', ') || '(없음)'
  try {
    await ElMessageBox.confirm(
      `신청 Scope: ${scopes}\n실제 부여 Scope는 카탈로그 정의와 교집합으로 서버가 결정합니다.`,
      `신청 ${app.app_id?.slice(0, 8)}을 승인하시겠습니까?`,
      { confirmButtonText: '승인', cancelButtonText: '취소' },
    )
  } catch {
    return
  }
  busy.value = true
  try {
    await api.patch(`/applications/${app.app_id}`, { action: 'APPROVE' })
    await reload()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '승인 실패')
  } finally {
    busy.value = false
  }
}

async function reject(app: any) {
  let comment: string
  try {
    const { value } = await ElMessageBox.prompt('반려 사유를 입력하세요.', '반려', {
      confirmButtonText: '반려',
      cancelButtonText: '취소',
    })
    comment = value
  } catch {
    return
  }
  busy.value = true
  try {
    await api.patch(`/applications/${app.app_id}`, { action: 'REJECT', reviewComment: comment })
    await reload()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '반려 실패')
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
h2 { margin-bottom: 0.8rem; }
.notice { margin-bottom: 1rem; }
.scope-chip { margin: 0 0.2rem 0.2rem 0; font-family: monospace; }
.muted { color: #888; font-size: 0.85rem; }
</style>
