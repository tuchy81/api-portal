<template>
  <div>
    <h2>API 카탈로그</h2>
    <div class="search-bar">
      <input v-model="query" placeholder="API 이름/코드 검색..." @input="search" class="search-input" />
    </div>
    <div v-if="loading">로딩 중...</div>
    <div v-else>
      <div v-for="api in apis" :key="api.api_id" class="card api-card">
        <div class="api-header">
          <h3>{{ api.name }}</h3>
          <span class="badge badge-active">{{ api.status }}</span>
        </div>
        <p class="api-code">{{ api.api_code }}</p>
        <p class="api-desc">{{ api.description }}</p>
        <p class="api-path">경로: <code>{{ api.public_path }}</code></p>
        <div class="api-actions">
          <router-link :to="`/cdp/catalog/${api.api_id}`" class="btn btn-primary">상세 보기</router-link>
          <router-link :to="`/cdp/apply/${api.api_id}`" class="btn btn-success" style="margin-left: 0.5rem">사용 신청</router-link>
        </div>
      </div>
      <p v-if="!apis.length">등록된 API가 없습니다.</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import api from '../api/axios'

const apis = ref<any[]>([])
const loading = ref(false)
const query = ref('')

async function search() {
  loading.value = true
  try {
    const { data } = await api.get('/catalog/apis', { params: { q: query.value || undefined } })
    apis.value = data.items
  } finally {
    loading.value = false
  }
}

onMounted(search)
</script>

<style scoped>
h2 { margin-bottom: 1.5rem; }
.search-bar { margin-bottom: 1rem; }
.search-input { width: 100%; max-width: 400px; padding: 0.5rem 1rem; border: 1px solid #ccc; border-radius: 6px; font-size: 0.9rem; }
.api-card { margin-bottom: 1rem; }
.api-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
.api-code { color: #666; font-size: 0.85rem; margin-bottom: 0.3rem; }
.api-desc { margin-bottom: 0.5rem; }
.api-path { font-size: 0.85rem; margin-bottom: 0.8rem; }
.api-path code { background: #f0f0f0; padding: 0.1rem 0.4rem; border-radius: 3px; }
</style>
