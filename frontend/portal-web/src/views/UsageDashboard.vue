<template>
  <div>
    <h2>사용량 대시보드</h2>
    <div class="card">
      <p>PAT Token ID: <input v-model="tokenId" class="token-input" placeholder="token_id 입력" @change="loadUsage" /></p>
      <div v-if="usage">
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
        <table style="margin-top:1rem">
          <thead><tr><th>날짜</th><th>호출수</th><th>오류수</th><th>평균 지연(ms)</th></tr></thead>
          <tbody>
            <tr v-for="d in usage.dailySeries" :key="d.date">
              <td>{{ d.date }}</td><td>{{ d.calls }}</td><td>{{ d.errors }}</td><td>{{ d.avgLatencyMs }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else-if="tokenId">로딩 중...</p>
      <p v-else>위에 Token ID를 입력하세요.</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import api from '../api/axios'

const tokenId = ref('')
const usage = ref<any>(null)

async function loadUsage() {
  if (!tokenId.value) return
  const { data } = await api.get(`/usage/tokens/${tokenId.value}`)
  usage.value = data
}

const todayPct = computed(() => {
  if (!usage.value) return 0
  return Math.min(100, (usage.value.liveToday.calls / usage.value.liveToday.quota) * 100)
})
const monthPct = computed(() => {
  if (!usage.value) return 0
  return Math.min(100, (usage.value.liveMonth.calls / usage.value.liveMonth.quota) * 100)
})
</script>

<style scoped>
h2 { margin-bottom:1rem; }
.token-input { padding:0.4rem 0.8rem; border:1px solid #ccc; border-radius:4px; width:300px; }
.quota-row { display:flex; gap:2rem; margin-top:1rem; }
.quota-box { flex:1; }
.quota-bar { background:#eee; border-radius:4px; height:20px; overflow:hidden; margin:0.5rem 0; }
.quota-fill { background:#1565c0; height:100%; transition:width 0.3s; }
.quota-fill.monthly { background:#4caf50; }
</style>
