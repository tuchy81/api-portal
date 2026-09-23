<template>
  <div>
    <h2>API 사용 신청</h2>
    <el-card shadow="never">
      <p v-if="apiDetail" class="api-name">{{ apiDetail.name }} <code>{{ apiDetail.public_path }}</code></p>

      <el-form :model="form" label-position="top" @submit.prevent="submit">
        <el-card shadow="never" class="scope-set">
          <template #header>
            스코프 선택 <span class="required">*</span>
          </template>
          <p class="hint">필요한 권한만 선택하세요. 승인 시 서버가 카탈로그 정의와 교집합으로 부여합니다.</p>
          <el-checkbox-group v-model="form.requestedScopes">
            <div v-for="s in availableScopes" :key="s.scope_id || s.scope_name" class="scope-option">
              <el-checkbox :value="s.scope_name">
                <code>{{ s.scope_name }}</code>
                <span class="scope-meta">{{ s.http_method }} {{ s.path_pattern }}</span>
                <span v-if="s.description" class="scope-desc">— {{ s.description }}</span>
              </el-checkbox>
            </div>
          </el-checkbox-group>
          <el-alert v-if="!availableScopes.length && apiDetail" type="warning" :closable="false" show-icon>
            이 API에 정의된 Scope가 없습니다. API 오너에게 문의하세요.
          </el-alert>
        </el-card>

        <el-form-item label="신청 목적" required>
          <el-input v-model="form.purpose" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="예상 TPS">
          <el-input-number v-model="form.expectedTps" :min="1" :max="100" />
        </el-form-item>
        <el-form-item label="일 예상 호출량">
          <el-input-number v-model="form.expectedDaily" :min="1" />
        </el-form-item>
        <el-form-item label="사용 기간 (종료일)" required>
          <el-date-picker v-model="form.validUntil" type="date" value-format="YYYY-MM-DD" placeholder="종료일 선택" />
        </el-form-item>

        <div class="form-actions">
          <el-button type="primary" native-type="submit" :loading="submitting" :disabled="!form.requestedScopes.length" @click="submit">
            신청 제출
          </el-button>
          <el-button @click="router.push('/cdp/catalog')">취소</el-button>
          <span v-if="!form.requestedScopes.length" class="warn">Scope를 1개 이상 선택하세요.</span>
        </div>
      </el-form>

      <el-alert v-if="result" type="success" :closable="false" show-icon class="result-box">
        <p>신청 완료! 신청 ID: <strong>{{ result.appId }}</strong></p>
        <p>상태: {{ result.status }}</p>
        <p v-if="result.eligibility?.missingRoles?.length">누락 권한: {{ result.eligibility.missingRoles.join(', ') }}</p>
      </el-alert>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import api from '../api/axios'

const route = useRoute()
const router = useRouter()
const submitting = ref(false)
const result = ref<any>(null)
const apiDetail = ref<any>(null)
const form = ref({
  purpose: '',
  expectedTps: 5,
  expectedDaily: 2000,
  validUntil: '2027-03-31',
  requestedScopes: [] as string[],
})

const availableScopes = computed<any[]>(() => apiDetail.value?.scopes || [])

onMounted(async () => {
  try {
    const { data } = await api.get(`/catalog/apis/${route.params.apiId}`)
    apiDetail.value = data
  } catch {
    ElMessage.error('API 정보를 불러오지 못했습니다.')
  }
})

async function submit() {
  if (!form.value.requestedScopes.length) return
  submitting.value = true
  try {
    const { data } = await api.post('/applications', {
      apiId: route.params.apiId,
      ...form.value,
    })
    result.value = data
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '신청 실패')
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.api-name { color: #555; margin: 0 0 1rem; }
.scope-set { margin-bottom: 1.2rem; }
.hint { font-size: 0.8rem; color: #666; margin: 0 0 0.8rem; }
.scope-option { margin-bottom: 0.6rem; font-size: 0.9rem; }
.scope-meta { color: #666; font-size: 0.78rem; margin-left: 0.4rem; }
.scope-desc { color: #888; font-size: 0.78rem; }
.required { color: #c62828; }
.warn { color: #e65100; font-size: 0.8rem; margin-left: 0.6rem; }
.form-actions { display: flex; align-items: center; gap: 0.5rem; margin-top: 1rem; }
.result-box { margin-top: 1.2rem; }
</style>
