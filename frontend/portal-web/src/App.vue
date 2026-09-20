<template>
  <div id="app">
    <nav class="navbar">
      <div class="brand">🏢 시민개발자 API 포털</div>
      <div class="nav-links">
        <router-link to="/cdp/catalog">카탈로그</router-link>
        <router-link to="/cdp/applications">내 신청</router-link>
        <router-link to="/cdp/tokens">PAT 관리</router-link>
        <router-link to="/cdp/usage">사용량</router-link>
        <span v-if="auth.isApiOwner || auth.isAdmin">
          <router-link to="/cdp/approvals">승인 수신함</router-link>
        </span>
        <span v-if="auth.isAdmin">
          <router-link to="/cdp/admin/audit">감사로그</router-link>
        </span>
        <span class="user-info" v-if="auth.username">{{ auth.username }}</span>
        <button v-if="auth.isAuthenticated" class="btn-logout" @click="auth.logout">로그아웃</button>
      </div>
    </nav>
    <main class="content">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useAuthStore } from './stores/auth'
const auth = useAuthStore()
onMounted(() => auth.fetchMe())
</script>

<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Noto Sans KR', sans-serif; background: #f5f6fa; }
.navbar { background: #1a1a2e; color: white; padding: 1rem 2rem; display: flex; justify-content: space-between; align-items: center; }
.navbar .brand { font-size: 1.2rem; font-weight: bold; }
.nav-links a { color: #ccc; text-decoration: none; margin-left: 1.5rem; }
.nav-links a.router-link-active { color: #4fc3f7; }
.nav-links .user-info { margin-left: 2rem; font-size: 0.85rem; color: #aaa; }
.btn-logout { margin-left: 1rem; padding: 0.3rem 0.8rem; background: transparent; border: 1px solid #666; border-radius: 4px; color: #ccc; cursor: pointer; font-size: 0.8rem; }
.btn-logout:hover { border-color: #aaa; color: white; }
.content { padding: 2rem; max-width: 1200px; margin: 0 auto; }
.card { background: white; border-radius: 8px; padding: 1.5rem; box-shadow: 0 2px 8px rgba(0,0,0,0.1); margin-bottom: 1rem; }
.btn { padding: 0.5rem 1.2rem; border: none; border-radius: 6px; cursor: pointer; font-size: 0.9rem; }
.btn-primary { background: #1565c0; color: white; }
.btn-danger { background: #c62828; color: white; }
.btn-success { background: #2e7d32; color: white; }
.badge { display: inline-block; padding: 0.2rem 0.6rem; border-radius: 12px; font-size: 0.75rem; font-weight: bold; }
.badge-active { background: #e8f5e9; color: #2e7d32; }
.badge-pending { background: #fff8e1; color: #f57f17; }
.badge-approved { background: #e3f2fd; color: #1565c0; }
.badge-revoked { background: #ffebee; color: #c62828; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 0.75rem; text-align: left; border-bottom: 1px solid #eee; font-size: 0.9rem; }
th { background: #f5f6fa; font-weight: 600; }
</style>
