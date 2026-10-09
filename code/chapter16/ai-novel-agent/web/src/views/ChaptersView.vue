<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { MagicStick, View, EditPen } from '@element-plus/icons-vue'
import { chapterApi, outlineApi } from '../api'

const route = useRoute()
const router = useRouter()
const projectId = route.params.projectId

const outlineChapters = ref([]) // 大纲章节（用于展示摘要）
const chapters = ref([]) // 已生成章节内容
const loading = ref(false)
const generatingIndex = ref(null) // 正在生成的章节序号

const generatedMap = computed(() => {
  const map = {}
  for (const c of chapters.value) map[c.chapter_index] = c
  return map
})

async function loadOutlineChapters() {
  try {
    const { data } = await outlineApi.listChapters(projectId)
    outlineChapters.value = data.items || []
  } catch {
    outlineChapters.value = []
  }
}

async function loadChapters() {
  try {
    const { data } = await chapterApi.list(projectId)
    chapters.value = data.items || []
  } catch {
    chapters.value = []
  }
}

async function generateChapter(orderIndex) {
  generatingIndex.value = orderIndex
  try {
    await chapterApi.generate(projectId, orderIndex)
    ElMessage.success(`第 ${orderIndex} 章生成完成`)
    await loadChapters()
  } catch {
    // 拦截器已提示
  } finally {
    generatingIndex.value = null
  }
}

function openChapter(orderIndex) {
  router.push({
    name: 'chapters',
    params: { projectId },
    query: { reading: String(orderIndex) },
  })
}

// 阅读模式：从 query 读取要展示的章节
const reading = ref(false)
const readingIndex = ref(null)
const readingChapter = ref(null)
const readingLoading = ref(false)

async function loadReading() {
  const idx = Number(route.query.reading)
  if (!idx) return
  readingIndex.value = idx
  reading.value = true
  readingLoading.value = true
  try {
    const { data } = await chapterApi.get(projectId, idx)
    readingChapter.value = data
  } catch {
    readingChapter.value = null
  } finally {
    readingLoading.value = false
  }
}

function closeReading() {
  reading.value = false
  readingChapter.value = null
  router.replace({ name: 'chapters', params: { projectId } })
}

// 编辑模式
const editing = ref(false)
const editForm = ref({ title: '', content: '', summary: '' })

function startEdit() {
  if (!readingChapter.value) return
  editForm.value = {
    title: readingChapter.value.title,
    content: readingChapter.value.content,
    summary: readingChapter.value.summary,
  }
  editing.value = true
}

async function saveEdit() {
  if (!editForm.value.content.trim()) {
    ElMessage.warning('正文不能为空')
    return
  }
  try {
    await chapterApi.patch(projectId, readingIndex.value, {
      expected_revision: readingChapter.value.current_revision,
      title: editForm.value.title,
      content: editForm.value.content,
      summary: editForm.value.summary,
    })
    ElMessage.success('保存成功')
    editing.value = false
    await loadReading()
    await loadChapters()
  } catch {
    // 拦截器已提示
  }
}

onMounted(() => {
  loadOutlineChapters()
  loadChapters()
  loadReading()
})
</script>

<template>
  <div>
    <div class="page-title-row">
      <h2 class="page-title">章节创作</h2>
      <div class="actions">
        <el-button link type="primary" @click="router.push({ name: 'memories', params: { projectId } })">
          写作记忆
        </el-button>
        <el-button link type="primary" @click="router.push({ name: 'outline', params: { projectId } })">
          返回大纲
        </el-button>
      </div>
    </div>

    <div v-loading="loading" class="page-container">
      <el-empty v-if="outlineChapters.length === 0" description="请先在大纲页生成大纲">
        <el-button type="primary" @click="router.push({ name: 'outline', params: { projectId } })">
          去生成大纲
        </el-button>
      </el-empty>

      <div v-else class="chapter-list">
        <div v-for="ch in outlineChapters" :key="ch.order_index" class="chapter-item">
          <div class="chapter-info">
            <div class="chapter-title">
              <span class="chapter-index">第 {{ ch.order_index }} 章</span>
              <span class="chapter-name">{{ ch.title }}</span>
            </div>
            <div class="chapter-summary">{{ ch.summary }}</div>
            <div v-if="ch.key_events && ch.key_events.length" class="chapter-events">
              <el-tag v-for="ev in ch.key_events" :key="ev" size="small" type="info" effect="plain">
                {{ ev }}
              </el-tag>
            </div>
          </div>
          <div class="chapter-actions">
            <template v-if="generatedMap[ch.order_index]">
              <el-tag size="small" type="success" effect="light" class="done-tag">
                已生成
              </el-tag>
              <el-button
                size="small"
                :icon="View"
                @click="openChapter(ch.order_index)"
              >
                阅读
              </el-button>
              <el-button
                size="small"
                type="primary"
                plain
                :icon="MagicStick"
                :loading="generatingIndex === ch.order_index"
                @click="generateChapter(ch.order_index)"
              >
                重新生成
              </el-button>
            </template>
            <el-button
              v-else
              type="primary"
              size="small"
              :icon="MagicStick"
              :loading="generatingIndex === ch.order_index"
              @click="generateChapter(ch.order_index)"
            >
              生成正文
            </el-button>
          </div>
        </div>
      </div>
    </div>

    <!-- 阅读弹窗 -->
    <el-dialog
      v-model="reading"
      :title="readingChapter ? readingChapter.title : `第 ${readingIndex} 章`"
      width="720px"
      top="5vh"
      destroy-on-close
      @closed="closeReading"
    >
      <div v-loading="readingLoading" class="reading-body">
        <template v-if="readingChapter">
          <div v-if="!editing" class="reading-content">
            <div class="chapter-meta">
              <el-tag size="small" effect="plain">
                {{ readingChapter.chapter_index }} / {{ outlineChapters.length }}
              </el-tag>
              <el-tag size="small" effect="plain" type="info">
                修订 {{ readingChapter.current_revision }}
              </el-tag>
            </div>
            <div class="content-text">{{ readingChapter.content }}</div>
          </div>

          <el-form v-else label-position="top">
            <el-form-item label="章节标题">
              <el-input v-model="editForm.title" />
            </el-form-item>
            <el-form-item label="正文">
              <el-input
                v-model="editForm.content"
                type="textarea"
                :rows="16"
                placeholder="正文内容"
              />
            </el-form-item>
            <el-form-item label="章节摘要（用于后续章节上下文）">
              <el-input v-model="editForm.summary" :rows="2" type="textarea" />
            </el-form-item>
          </el-form>
        </template>
        <el-empty v-else-if="!readingLoading" description="该章节尚未生成，请先在列表中生成" />
      </div>

      <template v-if="readingChapter" #footer>
        <template v-if="!editing">
          <el-button :icon="EditPen" @click="startEdit">编辑</el-button>
          <el-button type="primary" @click="reading = false">关闭</el-button>
        </template>
        <template v-else>
          <el-button @click="editing = false">取消</el-button>
          <el-button type="primary" @click="saveEdit">保存</el-button>
        </template>
      </template>
    </el-dialog>
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
  align-items: center;
}
.chapter-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.chapter-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  transition:
    border-color 0.2s,
    box-shadow 0.2s;
}
.chapter-item:hover {
  border-color: #409eff;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.1);
}
.chapter-info {
  flex: 1;
}
.chapter-title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}
.chapter-index {
  font-size: 13px;
  color: #409eff;
  font-weight: 600;
  background: #ecf5ff;
  padding: 2px 8px;
  border-radius: 4px;
}
.chapter-name {
  font-weight: 600;
  color: #303133;
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
.chapter-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.done-tag {
  margin-right: 4px;
}
.reading-body {
  min-height: 200px;
}
.reading-content {
  padding: 0 4px;
}
.chapter-meta {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.content-text {
  font-size: 15px;
  line-height: 1.9;
  color: #303133;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>