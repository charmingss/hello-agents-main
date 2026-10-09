<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { MagicStick } from '@element-plus/icons-vue'
import { memoryApi, outlineApi, chapterApi } from '../api'

const route = useRoute()
const router = useRouter()
const projectId = route.params.projectId

// ---------- 数据 ----------
const memories = ref([])
const loading = ref(false)
const outlineChapters = ref([]) // 大纲章节（供提取时选择）
const generatedIndexes = ref(new Set()) // 已生成章节的 order_index

const CATEGORY_LABELS = {
  character: '角色',
  world: '世界观',
  plot: '情节',
  foreshadowing: '伏笔',
  relation: '关系',
}
const CATEGORY_TYPES = {
  character: 'primary',
  world: 'success',
  plot: 'warning',
  foreshadowing: 'danger',
  relation: 'info',
}

// 状态过滤 + 分类过滤（前端内存过滤）
const statusFilter = ref('all')
const categoryFilter = ref('all')

const statusFilteredMemories = computed(() => {
  let list = memories.value
  if (statusFilter.value !== 'all') {
    list = list.filter((m) => m.status === statusFilter.value)
  }
  if (categoryFilter.value !== 'all') {
    list = list.filter((m) => m.category === categoryFilter.value)
  }
  return list
})

function applyFilters() {
  // 状态/分类均为前端过滤，无需重新请求
}

async function loadOutlineChapters() {
  try {
    const { data } = await outlineApi.listChapters(projectId)
    outlineChapters.value = data.items || []
  } catch {
    outlineChapters.value = []
  }
}

async function loadGenerated() {
  try {
    const { data } = await chapterApi.list(projectId)
    generatedIndexes.value = new Set((data.items || []).map((c) => c.chapter_index))
  } catch {
    generatedIndexes.value = new Set()
  }
}

async function loadMemories() {
  loading.value = true
  try {
    const { data } = await memoryApi.list(projectId)
    memories.value = data.items || []
  } catch {
    memories.value = []
  } finally {
    loading.value = false
  }
}

// ---------- 提取 ----------
const extractVisible = ref(false)
const extractChapter = ref(null)
const extracting = ref(false)

function openExtract() {
  if (outlineChapters.value.length === 0) {
    ElMessage.warning('请先生成大纲')
    return
  }
  extractChapter.value = null
  extractVisible.value = true
}

async function doExtract() {
  if (extractChapter.value === null) {
    ElMessage.warning('请选择要提取的章节')
    return
  }
  extracting.value = true
  try {
    const { data } = await memoryApi.extract(projectId, extractChapter.value)
    ElMessage.success(`提取完成：新增 ${data.extracted || 0} 条${data.superseded ? `，旧记忆 ${data.superseded} 条已归档` : ''}`)
    extractVisible.value = false
    await loadMemories()
    await loadGenerated()
  } catch {
    // 拦截器已提示
  } finally {
    extracting.value = false
  }
}

// ---------- 编辑 ----------
const editVisible = ref(false)
const editing = ref(false)
const editForm = ref({ memory: null, content: '' })

function openEdit(memory) {
  editForm.value = { memory, content: memory.content }
  editVisible.value = true
}

async function saveEdit() {
  const memory = editForm.value.memory
  if (!editForm.value.content.trim()) {
    ElMessage.warning('记忆内容不能为空')
    return
  }
  editing.value = true
  try {
    await memoryApi.patch(projectId, memory.id, {
      expected_revision: memory.current_revision,
      content: editForm.value.content.trim(),
    })
    ElMessage.success('记忆已更新')
    editVisible.value = false
    await loadMemories()
  } catch {
    // 拦截器已提示
  } finally {
    editing.value = false
  }
}

// 停用 / 恢复
async function toggleStatus(memory) {
  const next = memory.status === 'active' ? 'superseded' : 'active'
  try {
    await memoryApi.patch(projectId, memory.id, {
      expected_revision: memory.current_revision,
      status: next,
    })
    ElMessage.success(next === 'active' ? '已恢复为启用' : '已停用（后续提取不再复用）')
    await loadMemories()
  } catch {
    // 拦截器已提示
  }
}

function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  return d.toLocaleString('zh-CN', { hour12: false })
}

onMounted(() => {
  loadOutlineChapters()
  loadGenerated()
  loadMemories()
})
</script>

<template>
  <div>
    <div class="page-title-row">
      <h2 class="page-title">写作记忆</h2>
      <div class="actions">
        <el-button
          link
          type="primary"
          @click="router.push({ name: 'chapters', params: { projectId } })"
        >
          返回章节
        </el-button>
        <el-button type="primary" :icon="MagicStick" @click="openExtract">
          提取记忆
        </el-button>
      </div>
    </div>

    <!-- 过滤栏 -->
    <div class="filter-bar">
      <el-radio-group v-model="statusFilter" size="small" @change="applyFilters">
        <el-radio-button label="all">全部</el-radio-button>
        <el-radio-button label="active">启用中</el-radio-button>
        <el-radio-button label="superseded">已归档</el-radio-button>
      </el-radio-group>

      <el-select
        v-model="categoryFilter"
        size="small"
        style="width: 140px"
        @change="applyFilters"
      >
        <el-option label="全部分类" value="all" />
        <el-option v-for="(label, key) in CATEGORY_LABELS" :key="key" :label="label" :value="key" />
      </el-select>

      <el-tag v-if="memories.length" size="small" effect="plain" class="count-tag">
        共 {{ memories.length }} 条记忆
      </el-tag>
    </div>

    <div v-loading="loading" class="page-container">
      <el-empty
        v-if="!loading && memories.length === 0"
        description="还没有写作记忆，选择章节提取关键事实"
      >
        <el-button type="primary" :icon="MagicStick" @click="openExtract">提取记忆</el-button>
      </el-empty>

      <el-empty
        v-else-if="!loading && statusFilteredMemories.length === 0"
        :description="`当前筛选下没有${statusFilter === 'superseded' ? '已归档' : '启用中'}的记忆`"
      />

      <div v-else class="memory-list">
        <div v-for="m in statusFilteredMemories" :key="m.id" class="memory-item">
          <div class="memory-head">
            <el-tag size="small" :type="CATEGORY_TYPES[m.category]" effect="light">
              {{ CATEGORY_LABELS[m.category] || m.category }}
            </el-tag>
            <span class="chapter-tag">
              来源 · {{ m.chapter_index == null ? '范文' : `第 ${m.chapter_index} 章` }}
            </span>
            <el-tag
              v-if="m.status === 'active'"
              size="small"
              type="success"
              effect="plain"
            >
              启用中
            </el-tag>
            <el-tag v-else size="small" type="info" effect="plain">已归档</el-tag>
            <span class="memory-rev">rev {{ m.current_revision }}</span>
            <span class="memory-time">{{ formatTime(m.updated_at) }}</span>
          </div>
          <div class="memory-content">{{ m.content }}</div>
          <div class="memory-foot">
            <el-button link type="primary" size="small" @click="openEdit(m)">编辑</el-button>
            <el-button
              link
              :type="m.status === 'active' ? 'warning' : 'success'"
              size="small"
              @click="toggleStatus(m)"
            >
              {{ m.status === 'active' ? '停用' : '恢复启用' }}
            </el-button>
          </div>
        </div>
      </div>
    </div>

    <!-- 提取弹窗 -->
    <el-dialog v-model="extractVisible" title="从章节提取记忆" width="480px">
      <p class="dialog-tip">
        从选定章节正文中提取角色、世界观、情节、伏笔与关系等关键事实，写入写作记忆库。
        同章重复提取会归档旧记忆并写入新结果。
      </p>
      <el-form label-width="80px">
        <el-form-item label="选择章节">
          <el-select v-model="extractChapter" placeholder="请选择章节" style="width: 100%">
            <el-option
              v-for="ch in outlineChapters"
              :key="ch.order_index"
              :value="ch.order_index"
              :label="`第 ${ch.order_index} 章 · ${ch.title}`"
            >
              <span>{{ `第 ${ch.order_index} 章 · ${ch.title}` }}</span>
              <el-tag
                v-if="generatedIndexes.has(ch.order_index)"
                size="small"
                type="success"
                effect="plain"
                style="margin-left: 8px"
              >
                已生成
              </el-tag>
              <el-tag v-else size="small" type="info" effect="plain" style="margin-left: 8px">
                未生成
              </el-tag>
            </el-option>
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="extractVisible = false">取消</el-button>
        <el-button type="primary" :icon="MagicStick" :loading="extracting" @click="doExtract">
          开始提取
        </el-button>
      </template>
    </el-dialog>

    <!-- 编辑弹窗 -->
    <el-dialog v-model="editVisible" title="编辑记忆" width="560px">
      <el-form label-width="80px">
        <el-form-item label="内容">
          <el-input
            v-model="editForm.content"
            type="textarea"
            :rows="6"
            maxlength="2000"
            show-word-limit
            placeholder="记忆内容"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editing" @click="saveEdit">保存</el-button>
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
.filter-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.count-tag {
  margin-left: auto;
}
.memory-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.memory-item {
  padding: 14px 16px;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  transition:
    border-color 0.2s,
    box-shadow 0.2s;
}
.memory-item:hover {
  border-color: #409eff;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.1);
}
.memory-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.chapter-tag {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-size: 12px;
  color: #909399;
}
.memory-rev {
  font-size: 12px;
  color: #c0c4cc;
  margin-left: auto;
}
.memory-time {
  font-size: 12px;
  color: #c0c4cc;
}
.memory-content {
  font-size: 14px;
  line-height: 1.8;
  color: #303133;
  white-space: pre-wrap;
  word-break: break-word;
}
.memory-foot {
  display: flex;
  gap: 4px;
  margin-top: 8px;
  justify-content: flex-end;
}
.dialog-tip {
  font-size: 13px;
  color: #909399;
  margin: 0 0 12px;
}
</style>