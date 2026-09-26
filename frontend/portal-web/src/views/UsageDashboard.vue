<template>
  <div>
    <h2>사용량 대시보드</h2>
    <el-card shadow="never">
      <div class="controls">
        <span>PAT 선택:</span>
        <el-select v-model="tokenId" placeholder="토큰을 선택하세요" class="token-input" @change="loadUsage">
          <el-option v-for="t in myTokens" :key="t.token_id" :value="t.token_id" :label="`${t.token_name} (${t.token_id})`" />
        </el-select>
        <el-select v-model="days" class="days-input" @change="loadUsage">
          <el-option :value="7" label="최근 7일" />
          <el-option :value="30" label="최근 30일" />
          <el-option :value="90" label="최근 90일" />
        </el-select>
        <span v-if="myTokens.length === 0" class="hint">발급된 PAT이 없습니다.</span>
      </div>

      <div v-if="usage">
        <!-- 요약 지표 -->
        <el-row :gutter="16" class="stat-row">
          <el-col :span="4" :xs="12">
            <el-statistic title="총 호출" :value="summary.totalCalls" />
          </el-col>
          <el-col :span="4" :xs="12">
            <el-statistic title="오류" :value="summary.totalErrors" />
          </el-col>
          <el-col :span="4" :xs="12">
            <el-statistic title="오류율" :value="summary.errorRate * 100" :precision="2" suffix="%" :value-style="errorRateStyle" />
          </el-col>
          <el-col :span="4" :xs="12">
            <el-statistic title="평균 응답시간" :value="summary.avgLatencyMs" suffix="ms" />
          </el-col>
          <el-col :span="4" :xs="12">
            <el-statistic title="P95 응답시간" :value="summary.p95LatencyMs" suffix="ms" />
          </el-col>
        </el-row>

        <!-- 쿼터 -->
        <el-row :gutter="32" class="quota-row">
          <el-col :span="12">
            <h4>오늘 사용량</h4>
            <el-progress :percentage="todayPct" :stroke-width="16" />
            <p>{{ usage.liveToday.calls }} / {{ usage.liveToday.quota }}</p>
          </el-col>
          <el-col :span="12">
            <h4>이번 달 사용량</h4>
            <el-progress :percentage="monthPct" :stroke-width="16" color="#67c23a" />
            <p>{{ usage.liveMonth.calls }} / {{ usage.liveMonth.quota }}</p>
          </el-col>
        </el-row>

        <!-- 응답시간 추이 -->
        <div v-if="series.length" class="chart-block">
          <h4>응답시간 추이 <span class="legend"><i class="sw avg"></i>평균 <i class="sw p95"></i>P95</span></h4>
          <svg class="chart" :viewBox="`0 0 ${CHART_W} ${CHART_H}`" preserveAspectRatio="none">
            <line :x1="0" :y1="CHART_H - 1" :x2="CHART_W" :y2="CHART_H - 1" class="axis" />
            <polyline class="line p95" :points="p95Points" />
            <polyline class="line avg" :points="avgPoints" />
          </svg>
          <p class="chart-scale">최대 {{ fmtMs(latencyMax) }} · {{ series[0].date }} ~ {{ series[series.length - 1].date }}</p>
        </div>

        <!-- 오류율 추이 -->
        <div v-if="series.length" class="chart-block">
          <h4>일별 오류율</h4>
          <div class="err-bars">
            <div v-for="d in series" :key="d.date" class="err-col" :title="`${d.date} · ${fmtPct(d.errorRate)} (${d.errors}/${d.calls})`">
              <div class="err-fill" :style="{ height: errBarHeight(d.errorRate) }"></div>
            </div>
          </div>
        </div>

        <el-table :data="series" style="width:100%; margin-top:1rem">
          <el-table-column prop="date" label="날짜" width="110" />
          <el-table-column label="호출수"><template #default="{ row }">{{ fmtInt(row.calls) }}</template></el-table-column>
          <el-table-column label="오류수"><template #default="{ row }">{{ fmtInt(row.errors) }}</template></el-table-column>
          <el-table-column label="오류율">
            <template #default="{ row }"><span :class="row.errorRate > 0.05 ? 'err-high' : ''">{{ fmtPct(row.errorRate) }}</span></template>
          </el-table-column>
          <el-table-column label="평균 지연(ms)"><template #default="{ row }">{{ fmtMs(row.avgLatencyMs) }}</template></el-table-column>
          <el-table-column label="P95 지연(ms)"><template #default="{ row }">{{ fmtMs(row.p95LatencyMs) }}</template></el-table-column>
        </el-table>
        <p v-if="!series.length" class="hint-muted">집계된 일별 통계가 없습니다. (집계 배치는 매일 00:05 실행)</p>
      </div>
      <p v-else-if="tokenId">로딩 중...</p>
      <p v-else>위에서 PAT을 선택하세요.</p>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import api from '../api/axios'

interface DailyPoint {
  date: string
  calls: number
  errors: number
  errorRate: number
  avgLatencyMs: number
  p95LatencyMs: number
}

const CHART_W = 600
const CHART_H = 120

const tokenId = ref('')
const days = ref(30)
const usage = ref<any>(null)
const myTokens = ref<any[]>([])

onMounted(async () => {
  const { data } = await api.get('/tokens')
  myTokens.value = (data.items || []).filter((t: any) => t.status === 'ACTIVE')
})

async function loadUsage() {
  if (!tokenId.value) return
  usage.value = null
  const { data } = await api.get(`/usage/tokens/${tokenId.value}`, { params: { days: days.value } })
  usage.value = data
}

// 백엔드와 동일한 정의: 오류율 = errors / (calls + errors) — `calls`는 성공 건수
const rate = (calls: number, errors: number) => (calls + errors ? errors / (calls + errors) : 0)

const series = computed<DailyPoint[]>(() =>
  (usage.value?.dailySeries || []).map((d: any) => ({
    date: d.date,
    calls: d.calls ?? 0,
    errors: d.errors ?? 0,
    errorRate: d.errorRate ?? rate(d.calls ?? 0, d.errors ?? 0),
    avgLatencyMs: d.avgLatencyMs ?? 0,
    p95LatencyMs: d.p95LatencyMs ?? 0,
  }))
)

const summary = computed(() => {
  const s = usage.value?.summary
  if (s) return s
  // 백엔드가 summary를 주지 않는 경우 일별 계열에서 보수적으로 계산
  const rows = series.value
  const totalCalls = rows.reduce((a, d) => a + d.calls, 0)
  const totalErrors = rows.reduce((a, d) => a + d.errors, 0)
  return {
    totalCalls,
    totalErrors,
    errorRate: rate(totalCalls, totalErrors),
    avgLatencyMs: rows.length ? Math.round(rows.reduce((a, d) => a + d.avgLatencyMs, 0) / rows.length) : 0,
    p95LatencyMs: rows.length ? Math.max(...rows.map((d) => d.p95LatencyMs)) : 0,
  }
})

const errorRateStyle = computed(() => (summary.value.errorRate > 0.05 ? { color: '#c62828' } : {}))

const latencyMax = computed(() => {
  const vals = series.value.flatMap((d) => [d.avgLatencyMs, d.p95LatencyMs])
  return Math.max(1, ...vals)
})

function buildPoints(key: 'avgLatencyMs' | 'p95LatencyMs'): string {
  const rows = series.value
  if (!rows.length) return ''
  const stepX = rows.length > 1 ? CHART_W / (rows.length - 1) : 0
  return rows
    .map((d, i) => {
      const x = rows.length > 1 ? i * stepX : CHART_W / 2
      const y = CHART_H - 1 - (d[key] / latencyMax.value) * (CHART_H - 8)
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}

const avgPoints = computed(() => buildPoints('avgLatencyMs'))
const p95Points = computed(() => buildPoints('p95LatencyMs'))

function errBarHeight(rate: number): string {
  const maxRate = Math.max(0.01, ...series.value.map((d) => d.errorRate))
  return `${Math.max(2, (rate / maxRate) * 100)}%`
}

const todayPct = computed(() => {
  if (!usage.value) return 0
  return Math.min(100, (usage.value.liveToday.calls / usage.value.liveToday.quota) * 100)
})
const monthPct = computed(() => {
  if (!usage.value) return 0
  return Math.min(100, (usage.value.liveMonth.calls / usage.value.liveMonth.quota) * 100)
})

const fmtInt = (n: number) => (n ?? 0).toLocaleString()
const fmtPct = (r: number) => `${((r ?? 0) * 100).toFixed(2)}%`
const fmtMs = (n: number) => `${Math.round(n ?? 0)}ms`
</script>

<style scoped>
h2 { margin-bottom: 1rem; }
.controls { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 1rem; }
.token-input { width: 300px; }
.days-input { width: 140px; }
.hint { font-size: 0.8rem; color: #e65100; }
.hint-muted { font-size: 0.85rem; color: #888; margin-top: 0.8rem; }
.stat-row { margin-top: 0.5rem; }
.err-high { color: #c62828; }
.quota-row { margin-top: 1.5rem; }
.quota-row h4 { margin: 0 0 0.4rem; font-size: 0.9rem; }
.quota-row p { margin: 0.4rem 0 0; font-size: 0.85rem; color: #666; }
.chart-block { margin-top: 1.5rem; }
.chart-block h4 { font-size: 0.9rem; margin-bottom: 0.5rem; }
.legend { font-weight: normal; font-size: 0.75rem; color: #666; margin-left: 0.5rem; }
.sw { display: inline-block; width: 10px; height: 3px; margin: 0 0.3rem 0 0.6rem; vertical-align: middle; }
.sw.avg { background: #1565c0; }
.sw.p95 { background: #ef6c00; }
.chart { width: 100%; height: 120px; background: #fafafa; border-radius: 4px; }
.chart .axis { stroke: #ddd; stroke-width: 1; }
.chart .line { fill: none; stroke-width: 2; vector-effect: non-scaling-stroke; }
.chart .line.avg { stroke: #1565c0; }
.chart .line.p95 { stroke: #ef6c00; }
.chart-scale { font-size: 0.75rem; color: #888; margin-top: 0.3rem; }
.err-bars { display: flex; align-items: flex-end; gap: 2px; height: 70px; background: #fafafa; border-radius: 4px; padding: 0.3rem; }
.err-col { flex: 1; height: 100%; display: flex; align-items: flex-end; }
.err-fill { width: 100%; background: #ef9a9a; border-radius: 2px 2px 0 0; }
</style>
