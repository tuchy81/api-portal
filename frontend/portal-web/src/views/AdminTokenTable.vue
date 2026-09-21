<template>
  <div>
    <h2>전체 PAT 관리 (관리자)</h2>

    <!-- 조회 조건 -->
    <div class="card">
      <div class="filter-grid">
        <label>만료 임박 (일 이내)
          <input v-model.number="filters.expiringInDays" type="number" min="0" max="365" class="input" />
        </label>
        <label>상태
          <select v-model="filters.status" class="input">
            <option value="">전체</option>
            <option value="ACTIVE">ACTIVE</option>
            <option value="EXPIRED">EXPIRED</option>
            <option value="INACTIVE">INACTIVE</option>
            <option value="REVOKED">REVOKED</option>
          </select>
        </label>
        <label class="check-label">
          <input type="checkbox" v-model="filters.includeExpired" />
          이미 만료된 PAT 포함
        </label>
      </div>

      <label class="user-label">발급자 ID 목록 (줄바꿈 또는 콤마 구분)
        <textarea v-model="userSubsRaw" class="input" rows="3"
                  placeholder="u-test-001&#10;u-test-002, u-admin-001"></textarea>
      </label>
      <p class="hint">
        입력하면 해당 사용자들이 소유한 PAT만 조회합니다. 비워두면 전체 조회입니다.
        <span v-if="userSubs.length"> — {{ userSubs.length }}명 지정됨</span>
      </p>

      <button class="btn btn-primary" @click="search" :disabled="loading">
        {{ loading ? '조회 중...' : '조회' }}
      </button>
      <button class="btn" style="margin-left:0.5rem" @click="resetFilters">초기화</button>
    </div>

    <!-- 결과 -->
    <div class="card" v-if="searched">
      <div class="result-head">
        <strong>조회 결과 {{ total }}건</strong>
        <div class="bulk-actions">
          <button class="btn btn-danger" :disabled="!selected.length || revoking" @click="revokeSelected">
            선택 회수 ({{ selected.length }})
          </button>
          <button class="btn btn-danger" style="margin-left:0.4rem"
                  :disabled="!userSubs.length || revoking" @click="revokeByUsers">
            조회 사용자 PAT 일괄 회수
          </button>
          <button class="btn btn-danger" style="margin-left:0.4rem" :disabled="revoking" @click="revokeExpired">
            만료 PAT 일괄 회수
          </button>
        </div>
      </div>

      <table style="margin-top:1rem">
        <thead>
          <tr>
            <th style="width:32px">
              <input type="checkbox" :checked="allRevocableSelected" @change="toggleAll" />
            </th>
            <th>Token ID</th><th>이름</th><th>발급자</th><th>API</th>
            <th>상태</th><th>만료일</th><th>남은 일수</th><th>마지막 사용</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in items" :key="t.tokenId" :class="rowClass(t)">
            <td>
              <input type="checkbox" :value="t.tokenId" v-model="selected" :disabled="!isRevocable(t)" />
            </td>
            <td><code>{{ t.tokenId }}</code></td>
            <td>{{ t.tokenName }}</td>
            <td>{{ t.userName || '-' }}<br /><small class="muted">{{ t.userSub }}</small></td>
            <td>{{ t.apiName || t.apiCode || '-' }}</td>
            <td><span :class="`badge badge-${t.status?.toLowerCase()}`">{{ t.status }}</span></td>
            <td>{{ (t.expiresAt || '').slice(0,10) }}</td>
            <td :class="expiryClass(t)">{{ formatDays(t.daysUntilExpiry) }}</td>
            <td>{{ t.lastUsedAt ? t.lastUsedAt.slice(0,10) : '미사용' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!items.length" class="muted" style="margin-top:1rem">조건에 맞는 PAT이 없습니다.</p>

      <div v-if="total > size" class="pager">
        <button class="btn" :disabled="page <= 1" @click="goPage(page - 1)">이전</button>
        <span class="page-info">{{ page }} / {{ totalPages }}</span>
        <button class="btn" :disabled="page >= totalPages" @click="goPage(page + 1)">다음</button>
      </div>
    </div>

    <!-- 회수 확인 다이얼로그 -->
    <div v-if="pending" class="modal-overlay">
      <div class="modal">
        <h3>PAT 일괄 회수 확인</h3>
        <p class="warning-banner">⚠️ 되돌릴 수 없는 작업입니다. 회수된 PAT을 쓰는 스크립트는 즉시 401을 받습니다.</p>
        <p class="confirm-target">{{ pending.description }}</p>
        <label>회수 사유
          <input v-model="reason" class="input" placeholder="예: 퇴직자 일괄 정리 / 만료 토큰 정리" />
        </label>
        <div style="margin-top:1rem">
          <button class="btn btn-danger" :disabled="revoking" @click="confirmRevoke">
            {{ revoking ? '회수 중...' : '회수 실행' }}
          </button>
          <button class="btn" style="margin-left:0.5rem" :disabled="revoking" @click="pending = null">취소</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
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
const reason = ref('')

const userSubs = computed(() =>
  userSubsRaw.value.split(/[\n,]/).map((s) => s.trim()).filter(Boolean)
)

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size.value)))

// REVOKED는 이미 회수된 상태라 대상에서 제외
const isRevocable = (t: AdminToken) => t.status !== 'REVOKED'
const revocableIds = computed(() => items.value.filter(isRevocable).map((t) => t.tokenId))
const allRevocableSelected = computed(
  () => revocableIds.value.length > 0 && revocableIds.value.every((id) => selected.value.includes(id))
)

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
    alert(e.response?.data?.detail || 'PAT 조회 실패')
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

function toggleAll(e: Event) {
  selected.value = (e.target as HTMLInputElement).checked ? [...revocableIds.value] : []
}

function revokeSelected() {
  pending.value = {
    description: `선택한 PAT ${selected.value.length}건을 회수합니다.`,
    payload: { tokenIds: [...selected.value] },
  }
}

function revokeByUsers() {
  pending.value = {
    description: `사용자 ${userSubs.value.length}명(${userSubs.value.join(', ')})이 소유한 모든 PAT을 회수합니다.`,
    payload: { userSubs: [...userSubs.value] },
  }
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
}

async function confirmRevoke() {
  if (!pending.value) return
  revoking.value = true
  try {
    const { data } = await api.post('/admin/tokens/revoke', {
      ...pending.value.payload,
      ...(reason.value ? { reason: reason.value } : {}),
    })
    alert(`${data.revoked}건의 PAT을 회수했습니다.`)
    pending.value = null
    reason.value = ''
    await search()
  } catch (e: any) {
    alert(e.response?.data?.detail || 'PAT 회수 실패')
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

function rowClass(t: AdminToken) {
  return t.status === 'REVOKED' ? 'row-revoked' : ''
}
</script>

<style scoped>
h2 { margin-bottom:1rem; }
.filter-grid { display:flex; gap:1.5rem; align-items:flex-end; flex-wrap:wrap; margin-bottom:1rem; }
.filter-grid label { font-size:0.85rem; }
.check-label { display:flex; align-items:center; gap:0.4rem; padding-bottom:0.5rem; }
.user-label { display:block; font-size:0.85rem; margin-bottom:0.3rem; }
.input { display:block; width:100%; margin-top:0.3rem; padding:0.4rem 0.8rem; border:1px solid #ccc; border-radius:4px; font-size:0.9rem; }
.filter-grid .input { width:180px; }
.check-label .input { width:auto; }
textarea.input { resize:vertical; font-family:monospace; }
.hint { font-size:0.8rem; color:#666; margin:0.4rem 0 1rem; }
.result-head { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.5rem; }
.bulk-actions { display:flex; align-items:center; }
.muted { color:#888; font-size:0.8rem; }
.expiring { color:#e65100; font-weight:600; }
.expired { color:#c62828; font-weight:600; }
.row-revoked { opacity:0.55; }
.pager { margin-top:1rem; display:flex; align-items:center; gap:0.8rem; }
.page-info { font-size:0.85rem; color:#666; }
.modal-overlay { position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); display:flex; justify-content:center; align-items:center; z-index:1000; }
.modal { background:white; border-radius:8px; padding:2rem; max-width:560px; width:90%; }
.modal h3 { margin-bottom:1rem; }
.warning-banner { background:#fff3e0; border:1px solid #fb8c00; padding:0.8rem; border-radius:4px; margin-bottom:1rem; font-weight:bold; font-size:0.9rem; }
.confirm-target { font-size:0.9rem; margin-bottom:1rem; }
.modal label { display:block; font-size:0.85rem; }
</style>
