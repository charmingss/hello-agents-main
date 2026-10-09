<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { projectApi } from '../api'
import { useAppStore } from '../stores/app'

const router = useRouter()
const store = useAppStore()

const projects = ref([])
const loading = ref(false)
const createVisible = ref(false)
const createForm = ref({
  title: '',
  language: 'zh',
})
const creating = ref(false)

async function load() {
  loading.value = true
  try {
    const { data } = await projectApi.list()
    projects.value = data
  } finally {
    loading.value = false
  }
}

async function createProject() {
  if (!createForm.value.title.trim()) {
    ElMessage.warning('请输入项目标题')
    return
  }
  creating.value = true
  try {
    const { data } = await projectApi.create({
      title: createForm.value.title.trim(),
      language: createForm.value.language,
    })
    ElMessage.success('项目创建成功')
    createVisible.value = false
    createForm.value.title = ''
    projects.value.push(data)
  } finally {
    creating.value = false
  }
}

function openProject(project) {
  store.setCurrentProject(project)
  router.push({ name: 'outline', params: { projectId: project.id } })
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-title-row">
      <h2 class="page-title">项目列表</h2>
      <el-button type="primary" :icon="Plus" @click="createVisible = true">
        新建项目
      </el-button>
    </div>

    <div v-loading="loading" class="page-container">
      <el-empty v-if="!loading && projects.length === 0" description="还没有项目，点击右上角新建">
        <el-button type="primary" @click="createVisible = true">新建项目</el-button>
      </el-empty>

      <el-table v-else :data="projects" style="width: 100%" @row-click="openProject">
        <el-table-column prop="title" label="标题" min-width="180" />
        <el-table-column prop="slug" label="Slug" min-width="140" />
        <el-table-column prop="language" label="语言" width="120">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.language }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'active' ? 'success' : 'info'">
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="openProject(row)">进入</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="createVisible" title="新建项目" width="460px">
      <el-form label-width="70px">
        <el-form-item label="标题">
          <el-input v-model="createForm.title" placeholder="例如：青云记" />
        </el-form-item>
        <el-form-item label="语言">
          <el-select v-model="createForm.language" style="width: 100%">
            <el-option label="简体中文" value="zh-CN" />
            <el-option label="English" value="en" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="createProject">创建</el-button>
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
</style>