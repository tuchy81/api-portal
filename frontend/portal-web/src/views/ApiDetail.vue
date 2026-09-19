<template>
  <div v-if="apiDetail" class="card">
    <h2>{{ apiDetail.name }}</h2>
    <p>{{ apiDetail.description }}</p>
    <table style="margin-top:1rem">
      <tr><th>API 코드</th><td>{{ apiDetail.api_code }}</td></tr>
      <tr><th>공개 경로</th><td><code>{{ apiDetail.public_path }}</code></td></tr>
      <tr><th>소유 부서</th><td>{{ apiDetail.owner_dept }}</td></tr>
      <tr><th>상태</th><td><span :class="`badge badge-${apiDetail.status?.toLowerCase()}`">{{ apiDetail.status }}</span></td></tr>
    </table>
    <h3 style="margin-top:1.5rem">허용 Scope</h3>
    <table>
      <thead><tr><th>Scope</th><th>Method</th><th>Path Pattern</th><th>설명</th></tr></thead>
      <tbody>
        <tr v-for="s in apiDetail.scopes" :key="s.scope_id">
          <td><code>{{ s.scope_name }}</code></td>
          <td>{{ s.http_method }}</td>
          <td><code>{{ s.path_pattern }}</code></td>
          <td>{{ s.description }}</td>
        </tr>
      </tbody>
    </table>
    <div style="margin-top:1.5rem">
      <router-link :to="`/cdp/apply/${apiDetail.api_id}`" class="btn btn-success">사용 신청</router-link>
    </div>
  </div>
  <div v-else>로딩 중...</div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api/axios'

const route = useRoute()
const apiDetail = ref<any>(null)

onMounted(async () => {
  const { data } = await api.get(`/catalog/apis/${route.params.apiId}`)
  apiDetail.value = data
})
</script>
