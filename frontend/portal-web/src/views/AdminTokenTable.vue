<template>
  <div>
    <h2>전체 PAT 관리 (관리자)</h2>

    <!-- 조회 조건 -->
    <el-card shadow="never" class="filter-card">
      <div class="filter-grid">
        <div class="filter-item">
          <span class="filter-label">만료 임박 (일 이내)</span>
          <el-input-number v-model="filters.expiringInDays" :min="0" :max="365" />
        </div>
        <div class="filter-item">
          <span class="filter-label">상태</span>
          <el-select v-model="filters.status" clearable placeholder="전체" style="width:180px">
            <el-option label="ACTIVE" value="ACTIVE" />
            <el-option label="EXPIRED" value="EXPIRED" />
            <el-option label="INACTIVE" value="INACTIVE" />
            <el-option label="REVOKED" value="REVOKED" />
          </el-select>
        </div>
        <el-checkbox v-model="filters.includeExpired" class="check-item">이미 만료된 PAT 포함</el-checkbox>
      </div>

      <div class="user-label">발급자 ID 목록 (줄바꿈 또는 콤마 구분)</div>
      <el-input
        v-model="userSubsRaw"
        type="textarea"
        :rows="3"
        placeholder="u-test-001&#10;u-test-002, u-admin-001"
      />
      <p class="hint">
        입력하면 해당 사용자들이 소유한 PAT만 조회합니다. 비워두면 전체 조회입니다.
        <span v-if="userSubs.length"> — {{ userSubs.length }}명 지정됨</span>
      </p>

      <el-button type="primary" :loading="loading" @click="search">조회</el-button>
      <el-button @click="resetFilters">초기화</el-button>
    </el-card>

    <!-- 결과 -->
    <el-card v-if="searched" shadow="never" class="result-card">
      <div class="result-head">
        <strong>조회 결과 {{ total }}건</strong>
        <div class="bulk-actions">
          <el-button type="danger" :disabled="!selected.length || revoking" @click="revokeSelected">
            선택 회수 ({{ selected.length }})
          </el-button>
          <el-button type="danger" :disabled="!userSubs.length || revoking" @click="revokeByUsers">
            조회 사용자 PAT 일괄 회수
          </el-button>
          <el-button type="danger" :disabled="revoking" @click="revokeExpired">
            만료 PAT 일괄 회수
          </el-button>
        </div>
      </div>

      <el-table :data="items" style="width:100%; margin-top:1rem" row-key="tokenId" :row-class-name="rowClassName" @selection-change="onSelectionChange">
        <el-table-column type="selection" width="40" :selectable="isRevocable" />
        <el-table-column label="Token ID" width="120">
          <template #default="{ row }"><code>{{ row.tokenId }}</code></template>
        </el-table-column>
        <el-table-column prop="tokenName" label="이름" />
        <el-table-column label="발급자" width="150">
          <template #default="{ row }">{{ row.userName || '-' }}<br /><small class="muted">{{ row.userSub }}</small></template>
        </el-table-column>
        <el-table-column label="API">
          <template #default="{ row }">{{ row.apiName || row.apiCode || '-' }}</template>
        </el-table-column>
        <el-table-column label="상태" width="100">
          <template #default="{ row }"><el-tag :type="statusTagType(row.status)">{{ row.status }}</el-tag></template>
        </el-table-column>
        <el-table-column label="만료일" width="110">
          <template #default="{ row }">{{ (row.expiresAt || '').slice(0, 10) }}</template>
        </el-table-column>
        <el-table-column label="남은 일수" width="100">
          <template #default="{ row }"><span :class="expiryClass(row)">{{ formatDays(row.daysUntilExpiry) }}</span></template>
        </el-table-column>
        <el-table-column label="마지막 사용" width="110">
          <template #default="{ row }">{{ row.lastUsedAt ? row.lastUsedAt.slice(0, 10) : '미사용' }}</template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!items.length" description="조건에 맞는 PAT이 없습니다." />

      <el-pagination
        v-if="total > size"
        class="pager"
        background
        layout="prev, pager, next"
        :current-page="page"
        :page-size="size"
        :total="total"
        @current-change="goPage"
      />
    </el-card>

    <!-- 회수 확인 다이얼로그 -->
    <el-dialog v-model="showRevokeDialog" title="PAT 일괄 회수 확인" width="560px">
      <el-alert type="warning" :closable="false" show-icon class="warning-banner">
        되돌릴 수 없는 작업입니다. 회수된 PAT을 쓰는 스크립트는 즉시 401을 받습니다.
      </el-alert>
      <p class="confirm-target">{{ pending?.description }}</p>
      <el-form label-position="top">
        <el-form-item label="회수 사유">
          <el-input v-model="reason" placeholder="예: 퇴직자 일괄 정리 / 만료 토큰 정리" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button type="danger" :loading="revoking" @click="confirmRevoke">회수 실행</el-button>
        <el-button :disabled="revoking" @click="showRevokeDialog = false">취소</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api/axios'

interface AdminToken {
  tokenId: string
  tokenName: string
  userSub: string
  userName?: string
  apiCode?: string
  apiName?: string
  status: string
  issuedAt?: string
  expiresAt?: string
  lastUsedAt?: string | null
  daysUntilExpiry?: number | null
}

interface PendingRevoke {
  description: string
  payload: Record<string, any>
}

const filters = ref({ expiringInDays: 7, includeExpired: false, status: '' })
const userSubsRaw = ref('')
const items = ref<AdminToken[]>([])
const selected = ref<string[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(50)
const loading = ref(false)
const searched = ref(false)
const revoking = ref(false)
const pending = ref<PendingRevoke | null>(null)
const showRevokeDialog = ref(false)
const reason = ref('')

const userSubs = computed(() =>
  userSubsRaw.value.split(/[\n,]/).map((s) => s.trim()).filter(Boolean)
)

const isRevocable = (t: AdminToken) => t.status !== 'REVOKED'

function onSelectionChange(rows: AdminToken[]) {
  selected.value = rows.map((r) => r.tokenId)
}

function statusTagType(status: string) {
  const map: Record<string, string> = { ACTIVE: 'success', EXPIRED: 'info', INACTIVE: 'info', REVOKED: 'danger' }
  return map[status] || 'info'
}

async function search() {
  loading.value = true
  selected.value = []
  try {
    const params: Record<string, any> = { page: page.value, size: size.value }
    if (filters.value.expiringInDays !== null && filters.value.expiringInDays !== undefined) {
      params.expiringInDays = filters.value.expiringInDays
    }
    if (filters.value.includeExpired) params.includeExpired = true
    if (filters.value.status) params.status = filters.value.status
    if (userSubs.value.length) params.userSubs = userSubs.value.join(',')

    const { data } = await api.get('/admin/tokens', { params })
    items.value = data.items || []
    total.value = data.total ?? items.value.length
    searched.value = true
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || 'PAT 조회 실패')
  } finally {
    loading.value = false
  }
}

function goPage(p: number) {
  page.value = p
  search()
}

function resetFilters() {
  filters.value = { expiringInDays: 7, includeExpired: false, status: '' }
  userSubsRaw.value = ''
  page.value = 1
}

function revokeSelected() {
  pending.value = {
    description: `선택한 PAT ${selected.value.length}건을 회수합니다.`,
    payload: { tokenIds: [...selected.value] },
  }
  showRevokeDialog.value = true
}

function revokeByUsers() {
  pending.value = {
    description: `사용자 ${userSubs.value.length}명(${userSubs.value.join(', ')})이 소유한 모든 PAT을 회수합니다.`,
    payload: { userSubs: [...userSubs.value] },
  }
  showRevokeDialog.value = true
}

function revokeExpired() {
  pending.value = {
    description: userSubs.value.length
      ? `사용자 ${userSubs.value.length}명의 만료된 PAT을 모두 회수합니다.`
      : '만료된 PAT을 전부 회수합니다.',
    payload: {
      expiredOnly: true,
      ...(userSubs.value.length ? { userSubs: [...userSubs.value] } : {}),
    },
  }
  showRevokeDialog.value = true
}

async function confirmRevoke() {
  if (!pending.value) return
  revoking.value = true
  try {
    const { data } = await api.post('/admin/tokens/revoke', {
      ...pending.value.payload,
      ...(reason.value ? { reason: reason.value } : {}),
    })
    ElMessage.success(`${data.revoked}건의 PAT을 회수했습니다.`)
    showRevokeDialog.value = false
    pending.value = null
    reason.value = ''
    await search()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || 'PAT 회수 실패')
  } finally {
    revoking.value = false
  }
}

function formatDays(d: number | null | undefined) {
  if (d === null || d === undefined) return '-'
  if (d < 0) return `${Math.abs(d)}일 경과`
  if (d === 0) return '오늘 만료'
  return `${d}일`
}

function expiryClass(t: AdminToken) {
  const d = t.daysUntilExpiry
  if (d === null || d === undefined) return ''
  if (d < 0) return 'expired'
  if (d <= 7) return 'expiring'
  return ''
}

function rowClassName({ row }: { row: AdminToken }) {
  return row.status === 'REVOKED' ? 'row-revoked' : ''
}
</script>

<style scoped>
h2 { margin-bottom: 1rem; }
.filter-card { margin-bottom: 1rem; }
.filter-grid { display: flex; gap: 1.5rem; align-items: flex-end; flex-wrap: wrap; margin-bottom: 1rem; }
.filter-item { display: flex; flex-direction: column; gap: 0.3rem; font-size: 0.85rem; }
.check-item { padding-bottom: 0.4rem; }
.user-label { font-size: 0.85rem; margin-bottom: 0.3rem; }
.hint { font-size: 0.8rem; color: #666; margin: 0.4rem 0 1rem; }
.result-head { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem; }
.bulk-actions { display: flex; align-items: center; gap: 0.4rem; }
.muted { color: #888; font-size: 0.8rem; }
.expiring { color: #e65100; font-weight: 600; }
.expired { color: #c62828; font-weight: 600; }
:deep(.row-revoked) { opacity: 0.55; }
.pager { margin-top: 1rem; justify-content: flex-end; }
.warning-banner { margin-bottom: 1rem; font-weight: bold; }
.confirm-target { font-size: 0.9rem; margin-bottom: 1rem; }
</style>
