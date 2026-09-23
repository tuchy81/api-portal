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

        <el-divider content-position="left">경로 설정</el-divider>
        <el-form-item label="공개 경로 (Public Path)" required>
          <el-input v-model="form.publicPath" placeholder="예: /capi/v1/vendors" />
        </el-form-item>
        <el-form-item label="업스트림 URL" required>
          <el-input v-model="form.upstreamUrl" placeholder="예: http://internal-gw/api/v1/vendors" />
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

        <el-divider content-position="left">스코프 <span class="required">*</span></el-divider>
        <p class="hint">PAT 발급 시 부여할 수 있는 권한 단위입니다.</p>
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
          <el-button type="primary" native-type="submit" :loading="submitting" @click="submit">API 등록</el-button>
          <el-button @click="router.push('/cdp/catalog')">취소</el-button>
        </div>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
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
    ElMessage.error('OpenAPI 스펙 파일을 확인하세요: ' + specError.value)
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
.scope-row { display: flex; gap: 0.5rem; margin-bottom: 0.6rem; align-items: center; }
.scope-name { flex: 2; }
.scope-method { flex: 0 0 100px; }
.scope-path { flex: 2; }
.scope-desc { flex: 2; }
.form-actions { padding-top: 1rem; margin-top: 1rem; border-top: 1px solid #eee; display: flex; gap: 0.5rem; }
</style>
