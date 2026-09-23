<template>
  <div>
    <h2>API 편집</h2>
    <el-card v-if="loading" shadow="never" v-loading="true" class="loading-box" />
    <el-card v-else shadow="never">
      <el-form :model="form" label-position="top" @submit.prevent="submit">
        <el-divider content-position="left">기본 정보</el-divider>
        <el-form-item label="API 코드">
          <el-input :model-value="apiCode" disabled />
        </el-form-item>
        <el-form-item label="API 이름" required>
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="설명">
          <el-input v-model="form.description" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="담당 부서" required>
          <el-input v-model="form.ownerDept" />
        </el-form-item>
        <el-form-item label="상태">
          <el-select v-model="form.status">
            <el-option v-for="s in STATUSES" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>

        <el-divider content-position="left">경로 설정</el-divider>
        <p class="hint">공개 경로를 바꾸면 게이트웨이 라우트가 새 경로로 재생성됩니다(기존 라우트는 제거됨).</p>
        <el-form-item label="공개 경로 (Public Path)" required>
          <el-input v-model="form.publicPath" />
        </el-form-item>
        <el-form-item label="업스트림 URL" required>
          <el-input v-model="form.upstreamUrl" />
        </el-form-item>

        <el-divider content-position="left">OpenAPI 스펙</el-divider>
        <p v-if="hasExistingSpec && !openapiSpec" class="hint">기존에 등록된 스펙이 있습니다. 새 파일을 올리면 교체됩니다.</p>
        <el-form-item>
          <input type="file" accept="application/json,.json" @change="onSpecFile" />
        </el-form-item>
        <el-alert v-if="specError" type="error" :closable="false" show-icon>{{ specError }}</el-alert>
        <el-alert v-else-if="specSummary" type="success" :closable="false" show-icon>
          {{ specSummary.title }} (v{{ specSummary.version }}) — 경로 {{ specSummary.pathCount }}개 인식됨
        </el-alert>

        <el-divider content-position="left">스코프 <span class="required">*</span></el-divider>
        <p class="hint">저장하면 아래 목록으로 전체 교체됩니다.</p>
        <div v-for="(scope, i) in form.scopes" :key="i" class="scope-row">
          <el-input v-model="scope.scopeName" placeholder="스코프 (예: capi.vendor.read)" class="scope-name" />
          <el-select v-model="scope.httpMethod" class="scope-method">
            <el-option v-for="m in HTTP_METHODS" :key="m" :label="m" :value="m" />
          </el-select>
          <el-input v-model="scope.pathPattern" placeholder="경로 패턴 (예: /capi/v1/vendors)" class="scope-path" />
          <el-input v-model="scope.description" placeholder="설명" class="scope-desc" />
          <el-button :icon="Close" circle size="small" :disabled="form.scopes.length === 1" @click="removeScope(i)" />
        </div>
        <el-button :icon="Plus" @click="addScope">스코프 추가</el-button>

        <div class="form-actions">
          <el-button type="primary" native-type="submit" :loading="submitting" @click="submit">저장</el-button>
          <el-button @click="router.push(`/cdp/catalog/${apiId}`)">취소</el-button>
        </div>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Plus, Close } from '@element-plus/icons-vue'
import api from '../api/axios'

const HTTP_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
const STATUSES = ['DRAFT', 'PUBLISHED', 'DEPRECATED', 'RETIRED']

const route = useRoute()
const router = useRouter()
const apiId = route.params.apiId as string

const loading = ref(true)
const submitting = ref(false)
const apiCode = ref('')
const hasExistingSpec = ref(false)

const specError = ref('')
const specSummary = ref<{ title: string; version: string; pathCount: number } | null>(null)
const openapiSpec = ref<Record<string, any> | null>(null)

const form = ref({
  name: '',
  description: '',
  ownerDept: '',
  publicPath: '',
  upstreamUrl: '',
  status: 'DRAFT',
  scopes: [{ scopeName: '', httpMethod: 'GET', pathPattern: '', description: '' }],
})

onMounted(async () => {
  try {
    const { data } = await api.get(`/catalog/apis/${apiId}`)
    apiCode.value = data.api_code
    hasExistingSpec.value = !!data.openapi_spec
    form.value = {
      name: data.name,
      description: data.description || '',
      ownerDept: data.owner_dept,
      publicPath: data.public_path,
      upstreamUrl: data.upstream_url,
      status: data.status,
      scopes: data.scopes.length
        ? data.scopes.map((s: any) => ({
            scopeName: s.scope_name,
            httpMethod: s.http_method,
            pathPattern: s.path_pattern,
            description: s.description || '',
          }))
        : [{ scopeName: '', httpMethod: 'GET', pathPattern: '', description: '' }],
    }
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail?.message || 'API 정보를 불러오지 못했습니다.')
    router.push('/cdp/catalog')
  } finally {
    loading.value = false
  }
})

function onSpecFile(e: Event) {
  specError.value = ''
  specSummary.value = null
  openapiSpec.value = null

  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return

  file.text().then((text) => {
    try {
      const parsed = JSON.parse(text)
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        specError.value = 'OpenAPI 스펙은 JSON 객체여야 합니다.'
        return
      }
      openapiSpec.value = parsed
      specSummary.value = {
        title: parsed.info?.title || '(제목 없음)',
        version: parsed.info?.version || '-',
        pathCount: Object.keys(parsed.paths || {}).length,
      }
    } catch (err: any) {
      specError.value = `JSON 파싱 실패: ${err.message}`
    }
  })
}

function addScope() {
  form.value.scopes.push({ scopeName: '', httpMethod: 'GET', pathPattern: '', description: '' })
}

function removeScope(i: number) {
  form.value.scopes.splice(i, 1)
}

async function submit() {
  if (specError.value) {
    ElMessage.error('OpenAPI 스펙 파일을 확인하세요: ' + specError.value)
    return
  }
  submitting.value = true
  try {
    const { data } = await api.patch(`/catalog/apis/${apiId}`, {
      name: form.value.name,
      description: form.value.description,
      ownerDept: form.value.ownerDept,
      publicPath: form.value.publicPath,
      upstreamUrl: form.value.upstreamUrl,
      status: form.value.status,
      scopes: form.value.scopes,
      ...(openapiSpec.value ? { openapiSpec: openapiSpec.value } : {}),
    })
    if (data.warning) {
      ElMessage.warning(`저장되었지만 경고가 있습니다: ${data.warning}`)
    } else {
      ElMessage.success('저장되었습니다.')
    }
    router.push(`/cdp/catalog/${apiId}`)
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || 'API 저장 실패')
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
h2 { margin-bottom: 1.5rem; }
.required { color: #c62828; }
.hint { font-size: 0.8rem; color: #666; margin: 0 0 0.8rem; }
.scope-row { display: flex; gap: 0.5rem; margin-bottom: 0.6rem; align-items: center; }
.scope-name { flex: 2; }
.scope-method { flex: 0 0 100px; }
.scope-path { flex: 2; }
.scope-desc { flex: 2; }
.form-actions { padding-top: 1rem; margin-top: 1rem; border-top: 1px solid #eee; display: flex; gap: 0.5rem; }
.loading-box { min-height: 200px; }
</style>
