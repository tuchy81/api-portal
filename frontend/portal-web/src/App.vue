<template>
  <el-container class="app-shell">
    <el-header height="60px" class="app-header">
      <div class="brand">
        <el-icon :size="20"><OfficeBuilding /></el-icon>
        <span>시민개발자 API 포털</span>
      </div>

      <el-menu
        mode="horizontal"
        :ellipsis="false"
        background-color="transparent"
        text-color="#c9cdd4"
        active-text-color="#ffffff"
        :default-active="route.path"
        router
        class="app-menu"
      >
        <el-menu-item index="/cdp/catalog">카탈로그</el-menu-item>
        <el-menu-item index="/cdp/applications">내 신청</el-menu-item>
        <el-menu-item index="/cdp/tokens">PAT 관리</el-menu-item>
        <el-menu-item index="/cdp/usage">사용량</el-menu-item>
        <el-menu-item v-if="auth.isApiOwner || auth.isAdmin" index="/cdp/approvals">승인 수신함</el-menu-item>
        <el-menu-item v-if="auth.isAdmin" index="/cdp/admin/tokens">전체 PAT</el-menu-item>
        <el-menu-item v-if="auth.isAdmin" index="/cdp/admin/audit">감사로그</el-menu-item>
      </el-menu>

      <div class="header-actions">
        <span v-if="auth.username" class="user-info">
          <el-icon><User /></el-icon>{{ auth.username }}
        </span>
        <el-button v-if="auth.isAuthenticated" size="small" plain @click="auth.logout">로그아웃</el-button>
      </div>
    </el-header>

    <el-main class="app-main">
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { OfficeBuilding, User } from '@element-plus/icons-vue'
import { useAuthStore } from './stores/auth'

const auth = useAuthStore()
const route = useRoute()
onMounted(() => auth.fetchMe())
</script>

<style>
:root {
  --el-color-primary: #1565c0;
}
body {
  margin: 0;
  font-family: 'Noto Sans KR', -apple-system, BlinkMacSystemFont, sans-serif;
  background: #f5f6fa;
}
.app-shell { min-height: 100vh; }
.app-header {
  background: #1a1a2e;
  color: white;
  display: flex;
  align-items: center;
  gap: 1.5rem;
  padding: 0 1.5rem;
}
.app-header .brand {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 1.05rem;
  font-weight: bold;
  white-space: nowrap;
}
.app-menu {
  flex: 1;
  border-bottom: none !important;
}
.app-menu .el-menu-item {
  font-size: 0.9rem;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 1rem;
  white-space: nowrap;
}
.header-actions .user-info {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.85rem;
  color: #c9cdd4;
}
.app-main {
  max-width: 1200px;
  margin: 0 auto;
  width: 100%;
  padding: 1.5rem;
}
</style>
