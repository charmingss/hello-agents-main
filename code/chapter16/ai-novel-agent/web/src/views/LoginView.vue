<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Notebook } from '@element-plus/icons-vue'
import { projectApi } from '../api'
import { useAppStore } from '../stores/app'

const router = useRouter()
const store = useAppStore()

const token = ref('')
const loading = ref(false)

function enter() {
  const value = token.value.trim()
  if (!value) {
    ElMessage.warning('请输入访问 Token')
    return
  }
  loading.value = true
  // 用 projects 列表验证 Token 有效性
  projectApi
    .list()
    .then(() => {
      store.setToken(value)
      ElMessage.success('登录成功')
      router.push({ name: 'projects' })
    })
    .catch(() => {
      ElMessage.error('Token 无效或服务不可用')
    })
    .finally(() => {
      loading.value = false
    })
}
</script>

<template>
  <div class="login-page">
    <el-card class="login-card" shadow="always">
      <div class="login-logo">
        <el-icon :size="36" color="#409EFF"><Notebook /></el-icon>
        <h1>AI 小说助手</h1>
        <p class="subtitle">大纲生成 · 章节创作 · 故事管理</p>
      </div>
      <el-form @submit.prevent="enter">
        <el-form-item>
          <el-input
            v-model="token"
            type="password"
            show-password
            placeholder="请输入 OIDC 访问 Token"
            size="large"
            @keyup.enter="enter"
          />
        </el-form-item>
        <el-button
          type="primary"
          size="large"
          :loading="loading"
          class="login-btn"
          @click="enter"
        >
          进入
        </el-button>
      </el-form>
      <p class="hint">Token 仅保存在本地浏览器（localStorage），不会上传到第三方。</p>
    </el-card>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(160deg, #f0f6ff 0%, #f5f7fa 100%);
}
.login-card {
  width: 420px;
  padding: 12px 8px;
}
.login-logo {
  text-align: center;
  margin-bottom: 24px;
}
.login-logo h1 {
  font-size: 24px;
  margin: 12px 0 4px;
  color: #303133;
}
.subtitle {
  color: #909399;
  font-size: 14px;
  margin: 0;
}
.login-btn {
  width: 100%;
}
.hint {
  margin-top: 16px;
  color: #a8abb2;
  font-size: 12px;
  text-align: center;
}
</style>