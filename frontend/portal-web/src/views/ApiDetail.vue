<template>
  <div v-if="apiDetail">
    <el-card shadow="never">
      <div class="detail-header">
        <h2>{{ apiDetail.name }}</h2>
        <div v-if="canManage" class="manage-actions">
          <el-button :icon="Edit" @click="router.push(`/cdp/catalog/${apiDetail.api_id}/edit`)">편집</el-button>
          <el-popconfirm title="이 API를 삭제하시겠습니까? 이 작업은 되돌릴 수 없습니다." confirm-button-text="삭제" cancel-button-text="취소" @confirm="onDelete">
            <template #reference>
              <el-button type="danger" :icon="Delete" :loading="deleting">삭제</el-button>
            </template>
          </el-popconfirm>
        </div>
      </div>
      <p class="description">{{ apiDetail.description }}</p>

      <el-descriptions :column="2" border class="meta">
        <el-descriptions-item label="API 코드">{{ apiDetail.api_code }}</el-descriptions-item>
        <el-descriptions-item label="공개 경로"><code>{{ apiDetail.public_path }}</code></el-descriptions-item>
        <el-descriptions-item label="소유 부서">{{ apiDetail.owner_dept }}</el-descriptions-item>
        <el-descriptions-item label="상태">
          <el-tag :type="statusTagType">{{ apiDetail.status }}</el-tag>
        </el-descriptions-item>
      </el-descriptions>

      <h3 class="section-title">허용 Scope</h3>
      <el-table :data="apiDetail.scopes" border size="small">
        <el-table-column prop="scope_name" label="Scope"><template #default="{ row }"><code>{{ row.scope_name }}</code></template></el-table-column>
        <el-table-column prop="http_method" label="Method" width="100">
          <template #default="{ row }"><el-tag size="small" effect="plain">{{ row.http_method }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="path_pattern" label="Path Pattern"><template #default="{ row }"><code>{{ row.path_pattern }}</code></template></el-table-column>
        <el-table-column prop="description" label="설명" />
      </el-table>

      <h3 class="section-title">OpenAPI 스펙</h3>
      <div v-if="apiDetail.openapi_spec" ref="swaggerContainer" class="swagger-box"></div>
      <el-empty v-else description="등록된 스펙 없음" :image-size="60" />

      <div class="apply-actions">
        <el-button type="success" size="large" @click="router.push(`/cdp/apply/${apiDetail.api_id}`)">사용 신청</el-button>
      </div>
    </el-card>
  </div>
  <div v-else v-loading="true" class="loading-box"></div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Edit, Delete } from '@element-plus/icons-vue'
import api from '../api/axios'
import { useAuthStore } from '../stores/auth'
import 'swagger-ui-dist/swagger-ui.css'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const apiDetail = ref<any>(null)
const swaggerContainer = ref<HTMLElement | null>(null)
const deleting = ref(false)

// Mirrors the backend's _assert_can_modify (routers/catalog.py): only a
// platform-admin, or the API's own owner (owner_sub), may edit/delete it —
// merely holding the api-owner role isn't enough.
const canManage = computed(() =>
  !!apiDetail.value && (auth.isAdmin || (auth.isApiOwner && apiDetail.value.owner_sub === auth.sub))
)

const statusTagType = computed(() => {
  const map: Record<string, string> = { PUBLISHED: 'success', DRAFT: 'info', DEPRECATED: 'warning', RETIRED: 'danger' }
  return map[apiDetail.value?.status] || 'info'
})

onMounted(async () => {
  const { data } = await api.get(`/catalog/apis/${route.params.apiId}`)
  apiDetail.value = data

  if (data.openapi_spec) {
    await nextTick() // v-if above must render `swaggerContainer` first
    const { SwaggerUIBundle } = await import('swagger-ui-dist')
    SwaggerUIBundle({
      spec: data.openapi_spec,
      domNode: swaggerContainer.value,
      presets: [SwaggerUIBundle.presets.apis],
      layout: 'BaseLayout',
    })
  }
})

async function onDelete() {
  if (!apiDetail.value) return
  deleting.value = true
  try {
    await api.delete(`/catalog/apis/${apiDetail.value.api_id}`)
    ElMessage.success('API가 삭제되었습니다.')
    router.push('/cdp/catalog')
  } catch (e: any) {
    // CDP-4009: existing applications reference this API — the backend
    // already points the caller at the RETIRED-status path instead.
    ElMessage.error(e.response?.data?.detail || 'API 삭제 실패')
  } finally {
    deleting.value = false
  }
}
</script>

<style scoped>
.detail-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; }
.detail-header h2 { margin: 0; }
.manage-actions { display: flex; gap: 0.5rem; flex-shrink: 0; }
.description { color: #666; margin: 0.5rem 0 1.2rem; }
.meta { margin-bottom: 1.5rem; }
.section-title { margin: 1.5rem 0 0.8rem; font-size: 1.05rem; }
.swagger-box { margin-top: 0.8rem; border: 1px solid #e0e0e0; border-radius: 6px; overflow: hidden; }
.apply-actions { margin-top: 1.5rem; }
.loading-box { min-height: 200px; }
</style>
