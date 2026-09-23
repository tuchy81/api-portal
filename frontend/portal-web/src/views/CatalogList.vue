<template>
  <div>
    <div class="catalog-header">
      <h2>API 카탈로그</h2>
      <el-button v-if="auth.isApiOwner || auth.isAdmin" type="primary" :icon="Plus" @click="router.push('/cdp/catalog/new')">
        API 등록
      </el-button>
    </div>

    <el-input
      v-model="query"
      placeholder="API 이름/코드 검색..."
      class="search-input"
      clearable
      :prefix-icon="Search"
      @input="search"
    />

    <div v-loading="loading">
      <el-row :gutter="16">
        <el-col v-for="api in apis" :key="api.api_id" :span="12" class="api-col">
          <el-card class="api-card" shadow="hover">
            <div class="api-header">
              <h3>{{ api.name }}</h3>
              <el-tag type="success" effect="light">{{ api.status }}</el-tag>
            </div>
            <p class="api-code">{{ api.api_code }}</p>
            <p class="api-desc">{{ api.description }}</p>
            <p class="api-path">경로: <el-tag size="small" type="info">{{ api.public_path }}</el-tag></p>
            <div class="api-actions">
              <el-button type="primary" size="small" @click="router.push(`/cdp/catalog/${api.api_id}`)">상세 보기</el-button>
              <el-button type="success" size="small" @click="router.push(`/cdp/apply/${api.api_id}`)">사용 신청</el-button>
            </div>
          </el-card>
        </el-col>
      </el-row>
      <el-empty v-if="!loading && !apis.length" description="등록된 API가 없습니다." />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Plus, Search } from '@element-plus/icons-vue'
import api from '../api/axios'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()

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
.catalog-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.2rem; }
.catalog-header h2 { margin: 0; }
.search-input { max-width: 400px; margin-bottom: 1.2rem; }
.api-col { margin-bottom: 1rem; }
.api-card { height: 100%; }
.api-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; gap: 0.5rem; }
.api-header h3 { margin: 0; font-size: 1.05rem; }
.api-code { color: #888; font-size: 0.82rem; margin: 0 0 0.4rem; font-family: monospace; }
.api-desc { margin: 0 0 0.6rem; color: #444; min-height: 1.3rem; }
.api-path { font-size: 0.85rem; margin: 0 0 0.9rem; }
.api-actions { display: flex; gap: 0.5rem; }
</style>
