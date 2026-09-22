<template>
  <div>
    <h2>API 편집</h2>
    <div v-if="loading" class="card">로딩 중...</div>
    <div v-else class="card">
      <form @submit.prevent="submit">
        <section>
          <h3>기본 정보</h3>
          <label>API 코드
            <input :value="apiCode" class="input" disabled />
          </label>
          <label>API 이름 <span class="required">*</span>
            <input v-model="form.name" class="input" required />
          </label>
          <label>설명
            <textarea v-model="form.description" class="input" rows="3"></textarea>
          </label>
          <label>담당 부서 <span class="required">*</span>
            <input v-model="form.ownerDept" class="input" required />
          </label>
          <label>상태
            <select v-model="form.status" class="input">
              <option value="DRAFT">DRAFT</option>
              <option value="PUBLISHED">PUBLISHED</option>
              <option value="DEPRECATED">DEPRECATED</option>
              <option value="RETIRED">RETIRED</option>
            </select>
          </label>
        </section>

        <section>
          <h3>경로 설정</h3>
          <p class="hint">공개 경로를 바꾸면 게이트웨이 라우트가 새 경로로 재생성됩니다(기존 라우트는 제거됨).</p>
          <label>공개 경로 (Public Path) <span class="required">*</span>
            <input v-model="form.publicPath" class="input" required />
          </label>
          <label>업스트림 URL <span class="required">*</span>
            <input v-model="form.upstreamUrl" class="input" required />
          </label>
        </section>

        <section>
          <h3>OpenAPI 스펙</h3>
          <p v-if="hasExistingSpec && !openapiSpec" class="hint">기존에 등록된 스펙이 있습니다. 새 파일을 올리면 교체됩니다.</p>
          <input type="file" accept="application/json,.json" class="input" @change="onSpecFile" />
          <p v-if="specError" class="spec-error">⚠️ {{ specError }}</p>
          <p v-else-if="specSummary" class="spec-ok">
            ✅ {{ specSummary.title }} (v{{ specSummary.version }}) — 경로 {{ specSummary.pathCount }}개 인식됨
          </p>
        </section>

        <section>
          <h3>스코프 <span class="required">*</span></h3>
          <p class="hint">저장하면 아래 목록으로 전체 교체됩니다.</p>
          <div v-for="(scope, i) in form.scopes" :key="i" class="scope-row">
            <input v-model="scope.scopeName" class="input scope-name" placeholder="스코프 (예: capi.vendor.read)" required />
            <select v-model="scope.httpMethod" class="input scope-method">
              <option>GET</option><option>POST</option><option>PUT</option>
              <option>PATCH</option><option>DELETE</option>
            </select>
            <input v-model="scope.pathPattern" class="input scope-path" placeholder="경로 패턴 (예: /capi/v1/vendors)" required />
            <input v-model="scope.description" class="input scope-desc" placeholder="설명" />
            <button type="button" class="btn-remove" @click="removeScope(i)" :disabled="form.scopes.length === 1">✕</button>
          </div>
          <button type="button" class="btn btn-secondary" @click="addScope">+ 스코프 추가</button>
        </section>

        <div class="form-actions">
          <button type="submit" class="btn btn-primary" :disabled="submitting">
            {{ submitting ? '저장 중...' : '저장' }}
          </button>
          <button type="button" class="btn" @click="$router.push(`/cdp/catalog/${apiId}`)" style="margin-left:0.5rem">취소</button>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '../api/axios'

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
    alert(e.response?.data?.detail?.message || 'API 정보를 불러오지 못했습니다.')
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
    alert('OpenAPI 스펙 파일을 확인하세요: ' + specError.value)
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
      alert(`저장되었지만 경고가 있습니다: ${data.warning}`)
    } else {
      alert('저장되었습니다.')
    }
    router.push(`/cdp/catalog/${apiId}`)
  } catch (e: any) {
    alert(e.response?.data?.detail || 'API 저장 실패')
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
h2 { margin-bottom: 1.5rem; }
section { margin-bottom: 2rem; }
h3 { font-size: 1rem; font-weight: 600; margin-bottom: 1rem; padding-bottom: 0.4rem; border-bottom: 1px solid #eee; }
label { display: block; margin-bottom: 0.9rem; font-size: 0.9rem; color: #333; }
.input { display: block; width: 100%; margin-top: 0.3rem; padding: 0.45rem 0.8rem; border: 1px solid #ccc; border-radius: 4px; font-size: 0.9rem; }
.input:disabled { background: #f5f5f5; color: #888; }
textarea.input { resize: vertical; }
.required { color: #c62828; }
.hint { font-size: 0.8rem; color: #666; margin-bottom: 0.8rem; }
.spec-error { font-size: 0.85rem; color: #c62828; margin-top: 0.6rem; }
.spec-ok { font-size: 0.85rem; color: #2e7d32; margin-top: 0.6rem; }
.scope-row { display: flex; gap: 0.5rem; margin-bottom: 0.6rem; align-items: center; }
.scope-name { flex: 2; }
.scope-method { flex: 0 0 90px; }
.scope-path { flex: 2; }
.scope-desc { flex: 2; }
.btn-remove { flex: 0 0 28px; background: #ffebee; border: 1px solid #ef9a9a; border-radius: 4px; cursor: pointer; color: #c62828; font-size: 0.8rem; padding: 0.3rem; }
.btn-remove:disabled { opacity: 0.3; cursor: default; }
.btn-secondary { background: #e3f2fd; border: 1px solid #90caf9; color: #1565c0; padding: 0.4rem 1rem; border-radius: 6px; cursor: pointer; font-size: 0.85rem; margin-top: 0.3rem; }
.form-actions { padding-top: 1rem; border-top: 1px solid #eee; }
</style>
