<template>
  <div>
    <h2>API 등록</h2>
    <div class="card">
      <form @submit.prevent="submit">
        <section>
          <h3>기본 정보</h3>
          <label>API 코드 <span class="required">*</span>
            <input v-model="form.apiCode" class="input" placeholder="예: MDM-VENDOR" required />
          </label>
          <label>API 이름 <span class="required">*</span>
            <input v-model="form.name" class="input" placeholder="예: MDM 협력사 정보 API" required />
          </label>
          <label>설명
            <textarea v-model="form.description" class="input" rows="3" placeholder="API 용도 및 제공 데이터 설명"></textarea>
          </label>
          <label>담당 부서 <span class="required">*</span>
            <input v-model="form.ownerDept" class="input" placeholder="예: MDM운영팀" required />
          </label>
        </section>

        <section>
          <h3>경로 설정</h3>
          <label>공개 경로 (Public Path) <span class="required">*</span>
            <input v-model="form.publicPath" class="input" placeholder="예: /capi/v1/vendors" required />
          </label>
          <label>업스트림 URL <span class="required">*</span>
            <input v-model="form.upstreamUrl" class="input" placeholder="예: http://internal-gw/api/v1/vendors" required />
          </label>
        </section>

        <section>
          <h3>OpenAPI 스펙</h3>
          <p class="hint">OpenAPI 3.x JSON 파일을 업로드하면 카탈로그 상세에서 열람할 수 있습니다. (선택)</p>
          <input type="file" accept="application/json,.json" class="input" @change="onSpecFile" />
          <p v-if="specError" class="spec-error">⚠️ {{ specError }}</p>
          <p v-else-if="specSummary" class="spec-ok">
            ✅ {{ specSummary.title }} (v{{ specSummary.version }}) — 경로 {{ specSummary.pathCount }}개 인식됨
          </p>
        </section>

        <section>
          <h3>스코프 <span class="required">*</span></h3>
          <p class="hint">PAT 발급 시 부여할 수 있는 권한 단위입니다.</p>
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
            {{ submitting ? '등록 중...' : 'API 등록' }}
          </button>
          <button type="button" class="btn" @click="$router.push('/cdp/catalog')" style="margin-left:0.5rem">취소</button>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api/axios'

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
  upstreamUrl: '',
  scopes: [{ scopeName: '', httpMethod: 'GET', pathPattern: '', description: '' }],
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
    await api.post('/catalog/apis', {
      ...form.value,
      ...(openapiSpec.value ? { openapiSpec: openapiSpec.value } : {}),
    })
    alert('API가 등록되었습니다.')
    router.push('/cdp/catalog')
  } catch (e: any) {
    alert(e.response?.data?.detail || 'API 등록 실패')
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
