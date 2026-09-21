<template>
  <div>
    <h2>사용량 대시보드</h2>
    <div class="card">
      <p>PAT 선택:
        <select v-model="tokenId" class="token-input" @change="loadUsage">
          <option value="" disabled>토큰을 선택하세요</option>
          <option v-for="t in myTokens" :key="t.token_id" :value="t.token_id">
            {{ t.token_name }} ({{ t.token_id }})
          </option>
        </select>
        <select v-model.number="days" class="days-input" @change="loadUsage">
          <option :value="7">최근 7일</option>
          <option :value="30">최근 30일</option>
          <option :value="90">최근 90일</option>
        </select>
        <span v-if="myTokens.length === 0" class="hint">발급된 PAT이 없습니다.</span>
      </p>

      <div v-if="usage">
        <!-- 요약 지표 -->
        <div class="stat-row">
          <div class="stat-tile">
            <span class="stat-label">총 호출</span>
            <strong class="stat-value">{{ fmtInt(summary.totalCalls) }}</strong>
          </div>
          <div class="stat-tile">
            <span class="stat-label">오류</span>
            <strong class="stat-value">{{ fmtInt(summary.totalErrors) }}</strong>
          </div>
          <div class="stat-tile">
            <span class="stat-label">오류율</span>
            <strong class="stat-value" :class="errorRateClass">{{ fmtPct(summary.errorRate) }}</strong>
          </div>
          <div class="stat-tile">
            <span class="stat-label">평균 응답시간</span>
            <strong class="stat-value">{{ fmtMs(summary.avgLatencyMs) }}</strong>
          </div>
          <div class="stat-tile">
            <span class="stat-label">P95 응답시간</span>
            <strong class="stat-value">{{ fmtMs(summary.p95LatencyMs) }}</strong>
          </div>
        </div>

        <!-- 쿼터 -->
        <div class="quota-row">
          <div class="quota-box">
            <h4>오늘 사용량</h4>
            <div class="quota-bar">
              <div class="quota-fill" :style="{width: todayPct + '%'}"></div>
            </div>
            <p>{{ usage.liveToday.calls }} / {{ usage.liveToday.quota }}</p>
          </div>
          <div class="quota-box">
            <h4>이번 달 사용량</h4>
            <div class="quota-bar">
              <div class="quota-fill monthly" :style="{width: monthPct + '%'}"></div>
            </div>
            <p>{{ usage.liveMonth.calls }} / {{ usage.liveMonth.quota }}</p>
          </div>
        </div>

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

        <table style="margin-top:1rem">
          <thead>
            <tr>
              <th>날짜</th><th>호출수</th><th>오류수</th><th>오류율</th>
              <th>평균 지연(ms)</th><th>P95 지연(ms)</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="d in series" :key="d.date">
              <td>{{ d.date }}</td>
              <td>{{ fmtInt(d.calls) }}</td>
              <td>{{ fmtInt(d.errors) }}</td>
              <td :class="d.errorRate > 0.05 ? 'err-high' : ''">{{ fmtPct(d.errorRate) }}</td>
              <td>{{ fmtMs(d.avgLatencyMs) }}</td>
              <td>{{ fmtMs(d.p95LatencyMs) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="!series.length" class="hint-muted">집계된 일별 통계가 없습니다. (집계 배치는 매일 00:05 실행)</p>
      </div>
      <p v-else-if="tokenId">로딩 중...</p>
      <p v-else>위에서 PAT을 선택하세요.</p>
    </div>
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

const errorRateClass = computed(() => (summary.value.errorRate > 0.05 ? 'err-high' : ''))

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
h2 { margin-bottom:1rem; }
.token-input { padding:0.4rem 0.8rem; border:1px solid #ccc; border-radius:4px; width:300px; }
.days-input { padding:0.4rem 0.8rem; border:1px solid #ccc; border-radius:4px; margin-left:0.5rem; }
.hint { font-size:0.8rem; color:#e65100; margin-left:0.5rem; }
.hint-muted { font-size:0.85rem; color:#888; margin-top:0.8rem; }
.stat-row { display:flex; gap:1rem; margin-top:1rem; flex-wrap:wrap; }
.stat-tile { flex:1; min-width:120px; background:#f5f6fa; border-radius:6px; padding:0.8rem 1rem; }
.stat-label { display:block; font-size:0.75rem; color:#666; margin-bottom:0.3rem; }
.stat-value { font-size:1.3rem; color:#1a1a2e; }
.err-high { color:#c62828; }
.quota-row { display:flex; gap:2rem; margin-top:1.5rem; }
.quota-box { flex:1; }
.quota-bar { background:#eee; border-radius:4px; height:20px; overflow:hidden; margin:0.5rem 0; }
.quota-fill { background:#1565c0; height:100%; transition:width 0.3s; }
.quota-fill.monthly { background:#4caf50; }
.chart-block { margin-top:1.5rem; }
.chart-block h4 { font-size:0.9rem; margin-bottom:0.5rem; }
.legend { font-weight:normal; font-size:0.75rem; color:#666; margin-left:0.5rem; }
.sw { display:inline-block; width:10px; height:3px; margin:0 0.3rem 0 0.6rem; vertical-align:middle; }
.sw.avg { background:#1565c0; }
.sw.p95 { background:#ef6c00; }
.chart { width:100%; height:120px; background:#fafafa; border-radius:4px; }
.chart .axis { stroke:#ddd; stroke-width:1; }
.chart .line { fill:none; stroke-width:2; vector-effect:non-scaling-stroke; }
.chart .line.avg { stroke:#1565c0; }
.chart .line.p95 { stroke:#ef6c00; }
.chart-scale { font-size:0.75rem; color:#888; margin-top:0.3rem; }
.err-bars { display:flex; align-items:flex-end; gap:2px; height:70px; background:#fafafa; border-radius:4px; padding:0.3rem; }
.err-col { flex:1; height:100%; display:flex; align-items:flex-end; }
.err-fill { width:100%; background:#ef9a9a; border-radius:2px 2px 0 0; }
</style>
