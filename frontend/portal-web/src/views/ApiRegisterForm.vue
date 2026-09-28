<template>
  <div>
    <h2>API 등록</h2>
    <el-card shadow="never">
      <el-form :model="form" label-position="top" @submit.prevent="submit">
        <el-divider content-position="left">기본 정보</el-divider>
        <el-form-item label="API 코드" required>
          <el-input v-model="form.apiCode" placeholder="예: MDM-VENDOR" />
        </el-form-item>
        <el-form-item label="API 이름" required>
          <el-input v-model="form.name" placeholder="예: MDM 협력사 정보 API" />
        </el-form-item>
        <el-form-item label="설명">
          <el-input v-model="form.description" type="textarea" :rows="3" placeholder="API 용도 및 제공 데이터 설명" />
        </el-form-item>
        <el-form-item label="담당 부서" required>
          <el-input v-model="form.ownerDept" placeholder="예: MDM운영팀" />
        </el-form-item>

        <el-divider content-position="left">API 공개 주소 (Public Base)</el-divider>
        <p class="hint">이 API가 공개될 기준 주소입니다. Path Parameter는 아래 Endpoint의 Pattern에 넣습니다.</p>
        <el-form-item label="Public Base" required>
          <el-input v-model="form.publicPath" placeholder="예: /capi/v1/vendors" />
        </el-form-item>

        <el-divider content-position="left">OpenAPI 스펙</el-divider>
        <p class="hint">OpenAPI 3.x JSON 파일을 업로드하면 카탈로그 상세에서 열람할 수 있습니다. (선택)</p>
        <el-form-item>
          <input type="file" accept="application/json,.json" @change="onSpecFile" />
        </el-form-item>
        <el-alert v-if="specError" type="error" :closable="false" show-icon>{{ specError }}</el-alert>
        <el-alert v-else-if="specSummary" type="success" :closable="false" show-icon>
          {{ specSummary.title }} (v{{ specSummary.version }}) — 경로 {{ specSummary.pathCount }}개 인식됨
        </el-alert>

        <el-divider content-position="left">제공 Endpoint <span class="required">*</span></el-divider>
        <p class="hint">Method + Pattern + Upstream Full URL + Required Scope. Pattern은 Public Base에 붙는 상대 경로입니다 (예: '/', '/{id}'). 서로 다른 파라미터 이름(<code>{id}</code> vs <code>{vendorId}</code>)이나 trailing slash 차이도 동일 Endpoint로 취급됩니다.</p>
        <div class="endpoint-header">
          <span class="endpoint-method">Method</span>
          <span class="endpoint-path">Pattern</span>
          <span class="endpoint-upstream">Upstream Full URL</span>
          <span class="endpoint-scope">Required Scope</span>
          <span class="endpoint-desc">설명</span>
          <span class="endpoint-remove-spacer"></span>
        </div>
        <div v-for="(endpoint, i) in form.endpoints" :key="i" class="endpoint-row">
          <el-select v-model="endpoint.httpMethod" class="endpoint-method">
            <el-option v-for="m in HTTP_METHODS" :key="m" :label="m" :value="m" />
          </el-select>
          <el-input v-model="endpoint.pathPattern" placeholder="Pattern (예: /{id})" class="endpoint-path" />
          <el-input v-model="endpoint.upstreamUrl" placeholder="Upstream Full URL (예: http://vendor:8080/api/vendors/{id})" class="endpoint-upstream" />
          <el-input v-model="endpoint.requiredScope" placeholder="Required Scope (예: capi.vendor.read)" class="endpoint-scope" />
          <el-input v-model="endpoint.description" placeholder="설명" class="endpoint-desc" />
          <el-button :icon="Close" circle size="small" :disabled="form.endpoints.length === 1" @click="removeEndpoint(i)" />
        </div>
        <el-alert v-if="duplicateWarning" type="warning" :closable="false" show-icon class="dup-alert">
          {{ duplicateWarning }}
        </el-alert>
        <div v-for="(endpoint, i) in form.endpoints" :key="`preview-${i}`" class="preview-row">
          <span class="preview-method">{{ endpoint.httpMethod }}</span>
          <code class="preview-final">{{ finalPublicPath(endpoint.pathPattern) || '—' }}</code>
          <span class="preview-arrow">→</span>
          <code class="preview-upstream">{{ endpoint.upstreamUrl || '—' }}</code>
        </div>
        <el-button :icon="Plus" @click="addEndpoint">Endpoint 추가</el-button>

        <div class="form-actions">
          <el-button type="primary" native-type="submit" :loading="submitting" @click="submit">API 등록</el-button>
          <el-button @click="router.push('/cdp/catalog')">취소</el-button>
        </div>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Plus, Close } from '@element-plus/icons-vue'
import api from '../api/axios'

const HTTP_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']

const router = useRouter()
const submitting = ref(false)

const specError = ref('')
const specSummary = ref<{ title: string; version: string; pathCount: number } | null>(null)
const openapiSpec = ref<Record<string, any> | null>(null)

const form = ref({
  apiCode: '',
  name: '',
  description: '',
  ownerDept: '',
  publicPath: '',
  endpoints: [{ requiredScope: '', httpMethod: 'GET', pathPattern: '/', upstreamUrl: '', description: '' }],
})

function finalPublicPath(pattern: string): string {
  const base = (form.value.publicPath || '').replace(/\/$/, '')
  if (!base) return ''
  if (!pattern || pattern === '/') return base
  return base + '/' + pattern.replace(/^\//, '')
}

// Mirror of backend's _normalize_endpoint_pattern so the UI can flag dup
// entries before submit; the server re-runs the exact same normalization
// as the enforcement source of truth.
function normalizePattern(pattern: string): string {
  if (!pattern || !pattern.startsWith('/')) return pattern
  let n = pattern.replace(/\{[A-Za-z_][A-Za-z0-9_]*\}|:[A-Za-z_][A-Za-z0-9_]*/g, '{param}')
  if (n.length > 1 && n.endsWith('/')) n = n.replace(/\/+$/, '')
  return n
}

const duplicateWarning = computed(() => {
  const seen = new Map<string, number>()
  for (let i = 0; i < form.value.endpoints.length; i++) {
    const e = form.value.endpoints[i]
    if (!e.httpMethod || !e.pathPattern) continue
    const key = `${e.httpMethod.toUpperCase()} ${normalizePattern(e.pathPattern)}`
    if (seen.has(key)) {
      return `Endpoint #${i + 1}이(가) #${seen.get(key)! + 1}과 동일한 Route입니다 (${key}). 파라미터 이름과 trailing slash는 비교 시 무시됩니다.`
    }
    seen.set(key, i)
  }
  return ''
})

async function onSpecFile(e: Event) {
  specError.value = ''
  specSummary.value = null
  openapiSpec.value = null

  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return

  try {
    const parsed = JSON.parse(await file.text())
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
}

function addEndpoint() {
  form.value.endpoints.push({ requiredScope: '', httpMethod: 'GET', pathPattern: '/', upstreamUrl: '', description: '' })
}

function removeEndpoint(i: number) {
  form.value.endpoints.splice(i, 1)
}

async function submit() {
  if (specError.value) {
    ElMessage.error('OpenAPI 스펙 파일을 확인하세요: ' + specError.value)
    return
  }
  if (duplicateWarning.value) {
    ElMessage.error(duplicateWarning.value)
    return
  }
  submitting.value = true
  try {
    await api.post('/catalog/apis', {
      ...form.value,
      ...(openapiSpec.value ? { openapiSpec: openapiSpec.value } : {}),
    })
    ElMessage.success('API가 등록되었습니다.')
    router.push('/cdp/catalog')
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || 'API 등록 실패')
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
h2 { margin-bottom: 1.5rem; }
.required { color: #c62828; }
.hint { font-size: 0.8rem; color: #666; margin: 0 0 0.8rem; }
.endpoint-header { display: flex; gap: 0.5rem; margin-bottom: 0.4rem; align-items: center; font-size: 0.78rem; font-weight: 600; color: #555; padding: 0 0.2rem; }
.endpoint-header .endpoint-remove-spacer { flex: 0 0 32px; }
.endpoint-row { display: flex; gap: 0.5rem; margin-bottom: 0.6rem; align-items: center; }
.endpoint-method { flex: 0 0 100px; }
.endpoint-path { flex: 1.2; }
.endpoint-upstream { flex: 3; }
.endpoint-scope { flex: 1.8; }
.endpoint-desc { flex: 1.5; }
.dup-alert { margin: 0.4rem 0; }
.preview-row { display: flex; gap: 0.5rem; align-items: center; font-size: 0.8rem; color: #444; margin: 0.15rem 0; }
.preview-method { flex: 0 0 60px; font-weight: 600; color: #1e40af; }
.preview-final { flex: 1.2; word-break: break-all; }
.preview-arrow { color: #999; }
.preview-upstream { flex: 3; word-break: break-all; color: #444; }
.form-actions { padding-top: 1rem; margin-top: 1rem; border-top: 1px solid #eee; display: flex; gap: 0.5rem; }
</style>
