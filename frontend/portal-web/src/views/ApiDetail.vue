<template>
  <div v-if="apiDetail" class="card">
    <div class="detail-header">
      <h2>{{ apiDetail.name }}</h2>
      <div v-if="canManage" class="manage-actions">
        <router-link :to="`/cdp/catalog/${apiDetail.api_id}/edit`" class="btn btn-secondary">편집</router-link>
        <button class="btn btn-danger" :disabled="deleting" @click="onDelete">
          {{ deleting ? '삭제 중...' : '삭제' }}
        </button>
      </div>
    </div>
    <p>{{ apiDetail.description }}</p>
    <table style="margin-top:1rem">
      <tr><th>API 코드</th><td>{{ apiDetail.api_code }}</td></tr>
      <tr><th>공개 경로</th><td><code>{{ apiDetail.public_path }}</code></td></tr>
      <tr><th>소유 부서</th><td>{{ apiDetail.owner_dept }}</td></tr>
      <tr><th>상태</th><td><span :class="`badge badge-${apiDetail.status?.toLowerCase()}`">{{ apiDetail.status }}</span></td></tr>
    </table>
    <h3 style="margin-top:1.5rem">허용 Scope</h3>
    <table>
      <thead><tr><th>Scope</th><th>Method</th><th>Path Pattern</th><th>설명</th></tr></thead>
      <tbody>
        <tr v-for="s in apiDetail.scopes" :key="s.scope_id">
          <td><code>{{ s.scope_name }}</code></td>
          <td>{{ s.http_method }}</td>
          <td><code>{{ s.path_pattern }}</code></td>
          <td>{{ s.description }}</td>
        </tr>
      </tbody>
    </table>
    <h3 style="margin-top:1.5rem">OpenAPI 스펙</h3>
    <div v-if="specSummary" class="spec-box">
      <table>
        <tr><th>제목</th><td>{{ specSummary.title }}</td></tr>
        <tr><th>버전</th><td>{{ specSummary.version }}</td></tr>
        <tr><th>OpenAPI</th><td>{{ specSummary.openapi }}</td></tr>
        <tr><th>정의된 경로</th><td>{{ specSummary.pathCount }}개</td></tr>
      </table>
      <button class="btn btn-secondary" style="margin-top:0.8rem" @click="showRaw = !showRaw">
        {{ showRaw ? '원문 접기' : '원문 보기' }}
      </button>
      <pre v-if="showRaw" class="spec-raw">{{ specRaw }}</pre>
    </div>
    <p v-else class="no-spec">등록된 스펙 없음</p>

    <div style="margin-top:1.5rem">
      <router-link :to="`/cdp/apply/${apiDetail.api_id}`" class="btn btn-success">사용 신청</router-link>
    </div>
  </div>
  <div v-else>로딩 중...</div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '../api/axios'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const apiDetail = ref<any>(null)
const showRaw = ref(false)
const deleting = ref(false)

// Mirrors the backend's _assert_can_modify (routers/catalog.py): only a
// platform-admin, or the API's own owner (owner_sub), may edit/delete it —
// merely holding the api-owner role isn't enough.
const canManage = computed(() =>
  !!apiDetail.value && (auth.isAdmin || (auth.isApiOwner && apiDetail.value.owner_sub === auth.sub))
)

onMounted(async () => {
  const { data } = await api.get(`/catalog/apis/${route.params.apiId}`)
  apiDetail.value = data
})

async function onDelete() {
  if (!apiDetail.value) return
  if (!confirm(`'${apiDetail.value.name}'을(를) 삭제하시겠습니까? 이 작업은 되돌릴 수 없습니다.`)) return

  deleting.value = true
  try {
    await api.delete(`/catalog/apis/${apiDetail.value.api_id}`)
    alert('API가 삭제되었습니다.')
    router.push('/cdp/catalog')
  } catch (e: any) {
    // CDP-4009: existing applications reference this API — the backend
    // already points the caller at the RETIRED-status path instead.
    alert(e.response?.data?.detail || 'API 삭제 실패')
  } finally {
    deleting.value = false
  }
}

const specSummary = computed(() => {
  const spec = apiDetail.value?.openapi_spec
  if (!spec || typeof spec !== 'object') return null
  return {
    title: spec.info?.title || '(제목 없음)',
    version: spec.info?.version || '-',
    openapi: spec.openapi || spec.swagger || '-',
    pathCount: Object.keys(spec.paths || {}).length,
  }
})

const specRaw = computed(() =>
  apiDetail.value?.openapi_spec ? JSON.stringify(apiDetail.value.openapi_spec, null, 2) : ''
)
</script>

<style scoped>
.detail-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; }
.detail-header h2 { margin-bottom: 0; }
.manage-actions { display: flex; gap: 0.5rem; flex-shrink: 0; }
.spec-box { margin-top: 0.5rem; }
.no-spec { margin-top: 0.5rem; color: #888; font-size: 0.9rem; }
.spec-raw { background: #1a1a2e; color: #cfd8dc; padding: 1rem; border-radius: 6px; margin-top: 0.8rem; max-height: 420px; overflow: auto; font-size: 0.8rem; line-height: 1.45; }
.btn-secondary { background: #e3f2fd; border: 1px solid #90caf9; color: #1565c0; padding: 0.4rem 1rem; border-radius: 6px; cursor: pointer; font-size: 0.85rem; }
.btn-danger { background: #ffebee; border: 1px solid #ef9a9a; color: #c62828; padding: 0.4rem 1rem; border-radius: 6px; cursor: pointer; font-size: 0.85rem; }
.btn-danger:disabled { opacity: 0.5; cursor: default; }
</style>
