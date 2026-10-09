<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Notebook } from '@element-plus/icons-vue'
import { useAppStore } from './stores/app'

const route = useRoute()
const router = useRouter()
const store = useAppStore()

const isLoginPage = computed(() => route.name === 'login')

function logout() {
  store.setToken('')
  store.setCurrentProject(null)
  router.push({ name: 'login' })
}
</script>

<template>
  <div class="app-shell">
    <header v-if="!isLoginPage" class="app-header">
      <div class="brand">
        <el-icon :size="22" color="#409EFF"><Notebook /></el-icon>
        <span>AI 小说助手</span>
      </div>
      <nav class="nav">
        <router-link to="/projects" class="nav-link">项目</router-link>
        <template v-if="store.currentProject">
          <router-link
            :to="{ name: 'sources', params: { projectId: store.currentProject.id } }"
            class="nav-link"
          >
            范文
          </router-link>
          <router-link
            :to="{ name: 'outline', params: { projectId: store.currentProject.id } }"
            class="nav-link"
          >
            大纲
          </router-link>
          <router-link
            :to="{ name: 'chapters', params: { projectId: store.currentProject.id } }"
            class="nav-link"
          >
            章节
          </router-link>
          <router-link
            :to="{ name: 'memories', params: { projectId: store.currentProject.id } }"
            class="nav-link"
          >
            记忆
          </router-link>
          <span class="project-name">
            <el-tag size="small" effect="plain" type="primary">
              {{ store.currentProject.title }}
            </el-tag>
          </span>
        </template>
      </nav>
      <div class="header-right">
        <el-button link type="danger" @click="logout">退出</el-button>
      </div>
    </header>
    <main class="app-main">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: #f5f7fa;
}
.app-header {
  height: 56px;
  display: flex;
  align-items: center;
  padding: 0 24px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.04);
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 17px;
  font-weight: 600;
  color: #303133;
  margin-right: 40px;
}
.nav {
  display: flex;
  align-items: center;
  gap: 20px;
  flex: 1;
}
.nav-link {
  color: #606266;
  text-decoration: none;
  font-size: 14px;
  padding: 4px 2px;
  transition: color 0.2s;
}
.nav-link:hover {
  color: #409eff;
}
.nav-link.router-link-active {
  color: #409eff;
  font-weight: 600;
  border-bottom: 2px solid #409eff;
}
.project-name {
  margin-left: 12px;
}
.header-right {
  margin-left: auto;
}
.app-main {
  flex: 1;
  padding: 24px;
  max-width: 1200px;
  width: 100%;
  margin: 0 auto;
}
</style>