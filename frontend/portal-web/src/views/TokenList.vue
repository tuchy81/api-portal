<template>
  <div>
    <h2>PAT 관리</h2>
    <el-button type="primary" class="issue-btn" @click="showIssueDialog = true">새 PAT 발급</el-button>

    <!-- PAT 발급 다이얼로그 (spec 7.4 보안 UX 7개 요건) -->
    <el-dialog
      v-model="showIssueDialog"
      title="PAT 발급"
      width="600px"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :show-close="false"
    >
      <div v-if="!plainToken">
        <div class="field">
          <span class="field-label">신청한 API <em>(승인된 신청 중 선택)</em></span>
          <el-alert v-if="approvedApps.length === 0" type="warning" :closable="false" show-icon>
            승인된 신청이 없습니다. 먼저 API 신청 후 승인을 받으세요.
          </el-alert>
          <el-radio-group v-else v-model="form.appId" class="app-list">
            <el-radio
              v-for="app in approvedApps"
              :key="app.app_id"
              :value="app.app_id"
              border
              class="app-card"
            >
              <div class="app-body">
                <div class="app-head">
                  <strong>{{ app.api_name }}</strong>
                  <code v-if="app.api_code" class="chip">{{ app.api_code }}</code>
                </div>
                <div class="app-row" v-if="app.public_path">
                  <span class="k">주소</span><code>{{ app.public_path }}</code>
                </div>
                <div class="app-row">
                  <span class="k">부여 Scope</span>
                  <span class="scopes">
                    <code v-for="s in (app.granted_scopes || [])" :key="s" class="chip scope">{{ s }}</code>
                    <em v-if="!(app.granted_scopes || []).length">없음</em>
                  </span>
                </div>
                <div class="app-row" v-if="requestedOnly(app).length">
                  <span class="k">신청만 됨</span>
                  <span class="scopes">
                    <code v-for="s in requestedOnly(app)" :key="s" class="chip scope muted">{{ s }}</code>
                  </span>
                </div>
                <div class="app-row" v-if="app.purpose">
                  <span class="k">신청 목적</span><span class="purpose">{{ app.purpose }}</span>
                </div>
                <div class="app-meta">
                  <span v-if="app.reviewed_at">승인 {{ app.reviewed_at.slice(0, 10) }}</span>
                  <span v-if="app.valid_until">사용기한 {{ String(app.valid_until).slice(0, 10) }}</span>
                  <span v-if="app.expected_daily">예상 {{ app.expected_daily }}건/일</span>
                  <span class="appid">신청 {{ app.app_id?.slice(0, 8) }}</span>
                </div>
              </div>
            </el-radio>
          </el-radio-group>
        </div>
        <el-form label-position="top">
          <el-form-item label="토큰 이름">
            <el-input v-model="form.tokenName" />
          </el-form-item>
          <el-form-item label="유효기간 (일, 1~90)">
            <el-input-number v-model="form.validDays" :min="1" :max="90" />
          </el-form-item>
        </el-form>
        <el-alert type="warning" :closable="false" show-icon class="warning">
          발급된 PAT은 이 화면에서만 1회 확인 가능합니다. 안전한 곳에 보관하세요.
        </el-alert>
      </div>
      <div v-else class="token-display">
        <el-alert type="warning" :closable="false" show-icon class="warning-banner">
          이 창을 닫으면 다시 확인할 수 없습니다!
        </el-alert>
        <div class="token-box">
          <code>{{ plainToken }}</code>
        </div>
        <el-button type="primary" @click="copyToken">{{ copied ? '복사됨 ✓' : '클립보드 복사' }}</el-button>
        <el-checkbox v-model="confirmed" class="confirm-label">
          토큰을 안전한 곳에 보관했습니다. (.env 파일 권장, 소스코드 하드코딩 금지)
        </el-checkbox>
      </div>

      <template #footer>
        <template v-if="!plainToken">
          <el-button type="primary" :loading="issuing" @click="issueToken">발급</el-button>
          <el-button @click="closeDialog">취소</el-button>
        </template>
        <el-button v-else type="danger" :disabled="!confirmed" @click="closeDialog">닫기</el-button>
      </template>
    </el-dialog>

    <!-- PAT 목록 -->
    <el-card shadow="never">
      <el-table :data="tokenStore.tokens" style="width: 100%">
        <el-table-column label="Token ID" width="130">
          <template #default="{ row }"><code>{{ row.token_id }}</code></template>
        </el-table-column>
        <el-table-column prop="token_name" label="이름" />
        <el-table-column label="Scope">
          <template #default="{ row }">{{ (row.scopes || []).join(', ') }}</template>
        </el-table-column>
        <el-table-column label="상태" width="100">
          <template #default="{ row }"><el-tag :type="statusTagType(row.status)">{{ row.status }}</el-tag></template>
        </el-table-column>
        <el-table-column label="만료일" width="110">
          <template #default="{ row }">{{ row.expires_at?.slice(0, 10) }}</template>
        </el-table-column>
        <el-table-column label="작업" width="90">
          <template #default="{ row }">
            <el-button v-if="row.status === 'ACTIVE'" type="danger" size="small" @click="revoke(row.token_id)">폐기</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!tokenStore.tokens.length && !tokenStore.loading" description="발급된 PAT이 없습니다." />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useTokenStore } from '../stores/token'
import api from '../api/axios'

const tokenStore = useTokenStore()
const showIssueDialog = ref(false)
const plainToken = ref<string | null>(null)  // 로컬 전용, 스토어 저장 금지
const copied = ref(false)
const confirmed = ref(false)
const issuing = ref(false)
const approvedApps = ref<any[]>([])

const form = ref({ appId: '', tokenName: '자동화 스크립트', validDays: 90 })

async function loadApprovedApps() {
  const { data } = await api.get('/applications')
  approvedApps.value = (data.items || []).filter((a: any) => a.status === 'APPROVED')
}

// Scopes the applicant asked for that didn't survive approval (R-08 narrows the
// grant to requested ∩ published). Worth surfacing so the PAT's actual reach
// isn't mistaken for the original request.
function requestedOnly(app: any): string[] {
  const granted = new Set<string>(app.granted_scopes || [])
  return (app.requested_scopes || []).filter((s: string) => !granted.has(s))
}

function statusTagType(status: string) {
  const map: Record<string, string> = { ACTIVE: 'success', EXPIRED: 'info', INACTIVE: 'info', REVOKED: 'danger' }
  return map[status] || 'info'
}

onMounted(() => {
  tokenStore.fetchList()
  loadApprovedApps()
})

onUnmounted(() => {
  plainToken.value = null  // 명시적 소거
})

async function issueToken() {
  issuing.value = true
  try {
    const { data } = await api.post('/tokens', form.value)
    plainToken.value = data.token  // 화면 표시용 1회 보관
    if (data.validityWarning) ElMessage.warning(data.validityWarning)
    await tokenStore.fetchList()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || '발급 실패')
  } finally {
    issuing.value = false
  }
}

async function copyToken() {
  if (plainToken.value) {
    try {
      await navigator.clipboard.writeText(plainToken.value)
      copied.value = true
    } catch {
      ElMessage.error('수동으로 복사하세요: ' + plainToken.value)
    }
  }
}

async function closeDialog() {
  if (plainToken.value && !confirmed.value) {
    try {
      await ElMessageBox.confirm('토큰을 다시 확인할 수 없습니다.', '정말 닫겠습니까?', {
        confirmButtonText: '닫기',
        cancelButtonText: '취소',
        type: 'warning',
      })
    } catch {
      return
    }
  }
  plainToken.value = null  // 소거
  showIssueDialog.value = false
  confirmed.value = false
  copied.value = false
  form.value = { appId: '', tokenName: '자동화 스크립트', validDays: 90 }
}

async function revoke(tokenId: string) {
  try {
    await ElMessageBox.confirm(`PAT ${tokenId}을 폐기하시겠습니까?`, '확인', {
      confirmButtonText: '폐기',
      cancelButtonText: '취소',
      type: 'warning',
    })
  } catch {
    return
  }
  await tokenStore.revokeToken(tokenId)
}
</script>

<style scoped>
h2 { margin-bottom: 1rem; }
.issue-btn { margin-bottom: 1rem; }
.warning { margin: 1rem 0; }
.warning-banner { margin-bottom: 1rem; font-weight: bold; }
.token-box { background: #1a1a2e; border-radius: 4px; padding: 1rem; margin-bottom: 1rem; word-break: break-all; }
.token-box code { color: #4fc3f7; font-size: 0.85rem; }
.confirm-label { display: flex; align-items: center; margin: 1rem 0; font-size: 0.85rem; height: auto; white-space: normal; }

.field { margin-bottom: 0.9rem; }
.field-label { display: block; font-weight: 600; margin-bottom: 0.4rem; }
.field-label em { font-weight: 400; color: #666; font-style: normal; font-size: 0.8rem; }
/* el-radio-group's own default flex-wrap:wrap (meant for horizontal radio
   layout) isn't overridden by flex-direction:column alone — once content
   exceeds max-height it wraps into a second column instead of just
   overflowing vertically, which is what produced the horizontal scrollbar. */
.app-list { display: flex; flex-direction: column; flex-wrap: nowrap; gap: 0.5rem; max-height: 20rem; overflow-y: auto; overflow-x: hidden; padding-right: 0.2rem; width: 100%; }
/* See Login.vue's .user-item comment: el-radio's default margin-right:30px
   (zeroed only on :last-child) skews centering under el-radio-group's
   align-items:center — zero it on every card so they all align flush. */
.app-card { height: auto; width: 100%; margin: 0 !important; padding: 0.7rem 0.8rem; }
.app-card :deep(.el-radio__label) { width: 100%; white-space: normal; }
.app-body { flex: 1; min-width: 0; }
.app-head { display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap; margin-bottom: 0.35rem; }
.app-row { display: flex; gap: 0.5rem; font-size: 0.82rem; margin-bottom: 0.2rem; }
.app-row .k { color: #777; flex-shrink: 0; min-width: 4.5rem; }
.app-row code { background: #f1f3f5; border-radius: 3px; padding: 0 0.3rem; word-break: break-all; }
.scopes { display: flex; flex-wrap: wrap; gap: 0.25rem; }
.chip { background: #eceff1; border-radius: 3px; padding: 0.05rem 0.35rem; font-size: 0.78rem; }
.chip.scope { background: #e3f2fd; color: #0d47a1; }
.chip.scope.muted { background: #f5f5f5; color: #999; text-decoration: line-through; }
.purpose { color: #333; word-break: break-word; }
.app-meta { display: flex; flex-wrap: wrap; gap: 0.7rem; margin-top: 0.4rem; font-size: 0.75rem; color: #888; }
.app-meta .appid { font-family: monospace; }
</style>
