<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { MagicStick, Edit } from '@element-plus/icons-vue'
import { outlineApi } from '../api'

const route = useRoute()
const router = useRouter()
const projectId = route.params.projectId

const outline = ref(null)
const chapters = ref([])
const loading = ref(false)
const generating = ref(false)
const targetChapters = ref(10)
const userPrompt = ref('')

async function loadOutline() {
  loading.value = true
  try {
    const { data } = await outlineApi.get(projectId)
    outline.value = data
  } catch {
    outline.value = null
  } finally {
    loading.value = false
  }
}

async function loadChapters() {
  try {
    const { data } = await outlineApi.listChapters(projectId)
    chapters.value = data.items || []
  } catch {
    chapters.value = []
  }
}

async function generate() {
  generating.value = true
  try {
    await outlineApi.generate(
      projectId,
      targetChapters.value,
      userPrompt.value.trim() || undefined,
    )
    ElMessage.success('大纲生成成功')
    await loadOutline()
    await loadChapters()
  } catch {
    // 错误已由拦截器提示
  } finally {
    generating.value = false
  }
}

async function editOutline() {
  try {
    const { value: title } = await ElMessageBox.prompt(
      `当前标题：${outline.value?.title || ''}`,
      '修改大纲标题',
      { inputValue: outline.value?.title || '', inputValidator: (v) => !!v.trim() },
    )
    await outlineApi.patch(projectId, {
      expected_revision: outline.value.current_revision,
      title: title.trim(),
    })
    ElMessage.success('标题已更新')
    await loadOutline()
  } catch {
    // 用户取消
  }
}

function goChapters() {
  router.push({ name: 'chapters', params: { projectId } })
}

onMounted(() => {
  loadOutline()
  loadChapters()
})
</script>

<template>
  <div>
    <div class="page-title-row">
      <h2 class="page-title">故事大纲</h2>
      <div class="actions">
        <el-button :icon="Document" @click="editOutline">修改标题</el-button>
        <el-button
          type="primary"
          :icon="MagicStick"
          :loading="generating"
          @click="generate"
        >
          生成 / 重新生成大纲
        </el-button>
      </div>
    </div>

    <!-- 创作指令输入区：驱动大纲生成 + RAG 检索范文参考 -->
    <div class="prompt-box">
      <el-input
        v-model="userPrompt"
        type="textarea"
        :rows="2"
        maxlength="4000"
        resize="vertical"
        :disabled="generating"
        placeholder="创作指令（可选）：描述你想要的故事，例如「写一个赛博朋克世界中侦探寻找失踪 AI 的故事，主线为悬疑推理，主角性格孤僻但内心温暖」。留空则基于故事圣经生成。"
      />
      <div class="prompt-hint">
        指令会连同范文参考、人物记忆、风格画像一起驱动大纲生成；生成后可在章节页逐章扩写。
      </div>
    </div>

    <div v-loading="loading" class="page-container">
      <template v-if="outline">
        <div class="outline-head">
          <h3 class="outline-title">{{ outline.title }}</h3>
          <div class="outline-meta">
            <el-tag size="small" effect="plain">
              目标 {{ outline.target_chapters }} 章
            </el-tag>
            <el-tag size="small" effect="plain" type="info">
              rev {{ outline.current_revision }}
            </el-tag>
          </div>
        </div>
        <p class="outline-premise">{{ outline.premise }}</p>

        <el-divider content-position="left">章节列表</el-divider>

        <el-empty
          v-if="chapters.length === 0"
          description="大纲没有章节，请先生成"
        >
          <el-button type="primary" :loading="generating" @click="generate">
            生成大纲
          </el-button>
        </el-empty>

        <div v-else class="chapter-list">
          <div
            v-for="ch in chapters"
            :key="ch.id"
            class="chapter-card"
            @click="goChapters"
          >
            <div class="chapter-index">{{ ch.order_index }}</div>
            <div class="chapter-body">
              <div class="chapter-title">{{ ch.title }}</div>
              <div class="chapter-summary">{{ ch.summary }}</div>
              <div v-if="ch.key_events && ch.key_events.length" class="chapter-events">
                <el-tag
                  v-for="ev in ch.key_events"
                  :key="ev"
                  size="small"
                  type="info"
                  effect="plain"
                >
                  {{ ev }}
                </el-tag>
              </div>
            </div>
          </div>
        </div>
      </template>

      <el-empty v-else-if="!loading" description="还没有大纲">
        <div class="generate-box">
          <p>先生成故事大纲，再逐章创作正文</p>
          <div class="gen-row">
            <el-input-number
              v-model="targetChapters"
              :min="1"
              :max="500"
              :disabled="generating"
            />
            <el-button
              type="primary"
              :icon="MagicStick"
              :loading="generating"
              @click="generate"
            >
              生成大纲
            </el-button>
          </div>
        </div>
      </el-empty>
    </div>
  </div>
</template>

<style scoped>
.page-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.page-title {
  margin: 0;
}
.actions {
  display: flex;
  gap: 8px;
}
.prompt-box {
  margin-bottom: 16px;
  padding: 14px 16px;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  background: #fafbfc;
}
.prompt-hint {
  margin-top: 6px;
  font-size: 12px;
  color: #909399;
}
.outline-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.outline-title {
  margin: 0;
  font-size: 22px;
  color: #303133;
}
.outline-meta {
  display: flex;
  gap: 8px;
}
.outline-premise {
  color: #606266;
  font-size: 14px;
  line-height: 1.7;
  margin: 8px 0 0;
}
.chapter-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.chapter-card {
  display: flex;
  gap: 14px;
  padding: 14px 16px;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  cursor: pointer;
  transition:
    border-color 0.2s,
    box-shadow 0.2s;
}
.chapter-card:hover {
  border-color: #409eff;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.12);
}
.chapter-index {
  width: 32px;
  height: 32px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #ecf5ff;
  color: #409eff;
  font-weight: 600;
  border-radius: 6px;
  font-size: 14px;
}
.chapter-body {
  flex: 1;
}
.chapter-title {
  font-weight: 600;
  color: #303133;
  margin-bottom: 4px;
}
.chapter-summary {
  color: #909399;
  font-size: 13px;
  margin-bottom: 8px;
}
.chapter-events {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.gen-box {
  text-align: center;
  padding: 24px 0;
}
.gen-row {
  display: inline-flex;
  gap: 12px;
  align-items: center;
  margin-top: 12px;
}
</style>