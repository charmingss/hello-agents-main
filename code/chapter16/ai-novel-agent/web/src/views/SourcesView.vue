<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { MagicStick, Refresh, UploadFilled } from '@element-plus/icons-vue'
import { sourceApi } from '../api'

const route = useRoute()
const router = useRouter()
const projectId = route.params.projectId

// ---------- 数据 ----------
const sources = ref([])
const loading = ref(false)

const STATUS_META = {
  upload_pending: { label: '待上传', type: 'info' },
  uploaded: { label: '已上传', type: 'info' },
  accepted: { label: '已受理', type: 'primary' },
  parsing: { label: '解析中', type: 'warning' },
  parsed: { label: '已解析', type: 'warning' },
  chunks_ready: { label: '分片就绪', type: 'success' },
  failed: { label: '失败', type: 'danger' },
  quarantined: { label: '已隔离', type: 'danger' },
}

const TYPE_LABELS = {
  'text/plain': 'TXT',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'DOCX',
  'application/pdf': 'PDF',
}

function statusMeta(status) {
  return STATUS_META[status] || { label: status, type: 'info' }
}

function typeLabel(mediaType) {
  return TYPE_LABELS[mediaType] || mediaType
}

function formatSize(bytes) {
  if (bytes == null) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

function formatTime(iso) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}

async function loadSources(silent = false) {
  if (!silent) loading.value = true
  try {
    const { data } = await sourceApi.list(projectId, { limit: 100 })
    sources.value = data.items || []
  } catch {
    if (!silent) sources.value = []
  } finally {
    if (!silent) loading.value = false
  }
  syncPolling()
}

// 列表轮询：存在“解析中”的范文时每 3 秒静默刷新
let listTimer = null

function syncPolling() {
  const parsing = sources.value.some((s) => s.status === 'parsing')
  if (parsing && !listTimer) {
    listTimer = setInterval(() => loadSources(true), 3000)
  } else if (!parsing && listTimer) {
    clearInterval(listTimer)
    listTimer = null
  }
}

// ---------- 上传 ----------
const uploadVisible = ref(false)
const uploading = ref(false)
const uploadFile = ref(null)

const BASIS_OPTIONS = [
  { value: 'user_owned', label: '我拥有该作品' },
  { value: 'public_domain', label: '公有领域作品' },
  { value: 'licensed', label: '已获得授权' },
  { value: 'explicit_permission', label: '获得明确许可' },
]

const USE_OPTIONS = [
  { value: 'analysis', label: '内容分析' },
  { value: 'retrieval', label: '片段检索' },
  { value: 'abstract_style', label: '抽象风格参考' },
]

// 各授权基础要求的附加声明字段（后端按 basis 严格校验）
const BASIS_FIELDS = {
  user_owned: { rights_holder: false, license_identifier: false, evidence_reference: false },
  public_domain: { rights_holder: false, license_identifier: false, evidence_reference: true },
  licensed: { rights_holder: true, license_identifier: true, evidence_reference: false },
  explicit_permission: { rights_holder: true, license_identifier: false, evidence_reference: true },
}

const authForm = reactive({
  basis: 'user_owned',
  rights_holder: '',
  license_identifier: '',
  evidence_reference: '',
})
const permittedUses = ref(['analysis', 'retrieval', 'abstract_style'])
const requiredFields = computed(() => BASIS_FIELDS[authForm.basis])

function openUpload() {
  uploadFile.value = null
  authForm.basis = 'user_owned'
  authForm.rights_holder = ''
  authForm.license_identifier = ''
  authForm.evidence_reference = ''
  permittedUses.value = ['analysis', 'retrieval', 'abstract_style']
  uploadVisible.value = true
}

function onFileChange(file) {
  uploadFile.value = file.raw || null
}

function onFileRemove() {
  uploadFile.value = null
}

const MEDIA_BY_EXT = {
  txt: 'text/plain',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  pdf: 'application/pdf',
}

async function sha256Hex(file) {
  const buffer = await file.arrayBuffer()
  const digest = await crypto.subtle.digest('SHA-256', buffer)
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

function validateUploadForm() {
  if (!uploadFile.value) {
    ElMessage.warning('请选择要上传的文件')
    return false
  }
  const ext = uploadFile.value.name.split('.').pop().toLowerCase()
  if (!MEDIA_BY_EXT[ext]) {
    ElMessage.warning('仅支持 .txt / .docx / .pdf 文件')
    return false
  }
  if (permittedUses.value.length === 0) {
    ElMessage.warning('请至少选择一种允许的用途')
    return false
  }
  if (requiredFields.value.rights_holder && !authForm.rights_holder.trim()) {
    ElMessage.warning('请填写权利持有人')
    return false
  }
  if (requiredFields.value.license_identifier && !authForm.license_identifier.trim()) {
    ElMessage.warning('请填写许可标识')
    return false
  }
  if (requiredFields.value.evidence_reference && !authForm.evidence_reference.trim()) {
    ElMessage.warning('请填写证据链接或来源说明')
    return false
  }
  return true
}

async function doUpload() {
  if (!validateUploadForm()) return
  if (!window.crypto?.subtle) {
    ElMessage.error('当前浏览器环境不支持计算文件哈希（需 HTTPS 或 localhost 访问）')
    return
  }
  uploading.value = true
  try {
    const file = uploadFile.value
    const ext = file.name.split('.').pop().toLowerCase()
    const sha = await sha256Hex(file)

    // 1. 发起上传，取得直传地址
    const { data: init } = await sourceApi.initiateUpload(projectId, {
      request_id: crypto.randomUUID(),
      filename: file.name,
      media_type: MEDIA_BY_EXT[ext],
      byte_size: file.size,
      content_sha256: sha,
      authorization: {
        basis: authForm.basis,
        permitted_uses: [...permittedUses.value],
        ...(requiredFields.value.rights_holder
          ? { rights_holder: authForm.rights_holder.trim() }
          : {}),
        ...(requiredFields.value.license_identifier
          ? { license_identifier: authForm.license_identifier.trim() }
          : {}),
        ...(requiredFields.value.evidence_reference
          ? { evidence_reference: authForm.evidence_reference.trim() }
          : {}),
      },
    })

    // 2. 直传文件内容到对象存储
    const putResp = await fetch(init.upload_url, {
      method: 'PUT',
      headers: init.required_headers || {},
      body: file,
    })
    if (!putResp.ok) {
      throw new UploadStepError(`文件直传失败（HTTP ${putResp.status}）`)
    }

    // 3. 确认上传完成
    const { data: done } = await sourceApi.completeUpload(projectId, init.upload_id, {
      upload_id: init.upload_id,
      byte_size: file.size,
      content_sha256: sha,
    })

    uploadVisible.value = false
    ElMessage.success('范文上传成功')
    await loadSources()

    // 4. 尝试自动开始解析
    try {
      await sourceApi.startIngestion(projectId, done.source_id)
      ElMessage.success('解析已启动，完成后可发起 AI 分析')
      await loadSources(true)
    } catch {
      ElMessage.warning('当前暂不能自动解析，请在列表中点击「开始解析」')
    }
  } catch (error) {
    if (error instanceof UploadStepError) {
      ElMessage.error(error.message)
    }
    // 其余错误由 axios 拦截器统一提示
  } finally {
    uploading.value = false
  }
}

class UploadStepError extends Error {}

// ---------- 解析 ----------
async function startParse(row) {
  try {
    await sourceApi.startIngestion(projectId, row.source_id)
    ElMessage.success('解析已启动')
    await loadSources(true)
  } catch {
    // 拦截器已提示
  }
}

// ---------- AI 分析（异步任务 + 进度轮询） ----------
const jobVisible = ref(false)
const job = reactive({
  source: null,
  job_id: null,
  status: '',
  total_batches: 0,
  completed_batches: 0,
  failed_batches: 0,
  error_code: null,
})
let jobTimer = null

const JOB_STATUS_LABELS = {
  pending: '排队中',
  running: '分析中',
  succeeded: '已完成',
  failed: '失败',
}

const jobPercentage = computed(() => {
  if (job.status === 'succeeded') return 100
  if (!job.total_batches) return 0
  return Math.min(100, Math.round((job.completed_batches / job.total_batches) * 100))
})

async function startAnalysis(row) {
  try {
    const { data } = await sourceApi.createAnalysisJob(projectId, row.source_id)
    Object.assign(job, {
      source: row,
      job_id: data.job_id,
      status: data.status,
      total_batches: data.total_batches,
      completed_batches: data.completed_batches,
      failed_batches: data.failed_batches,
      error_code: data.error_code,
    })
    jobVisible.value = true
    startJobPolling()
  } catch (error) {
    const code = error?.response?.data?.detail?.code || ''
    if (error?.response?.status === 409 && code.startsWith('analysis_job_')) {
      ElMessage.warning('该范文已有分析任务在进行中，请稍后再试')
      return
    }
    // 其余错误拦截器已提示
  }
}

function startJobPolling() {
  stopJobPolling()
  jobTimer = setInterval(pollJob, 2000)
}

function stopJobPolling() {
  if (jobTimer) {
    clearInterval(jobTimer)
    jobTimer = null
  }
}

async function pollJob() {
  if (!job.source || !job.job_id) return
  try {
    const { data } = await sourceApi.getAnalysisJob(projectId, job.source.source_id, job.job_id)
    job.status = data.status
    job.total_batches = data.total_batches
    job.completed_batches = data.completed_batches
    job.failed_batches = data.failed_batches
    job.error_code = data.error_code
    if (data.status === 'succeeded') {
      stopJobPolling()
      ElMessage.success('范文分析完成，可提取写作记忆')
      await loadSources(true)
    } else if (data.status === 'failed') {
      stopJobPolling()
      await loadSources(true)
    }
  } catch {
    // 单次轮询失败不中断，等待下一轮
  }
}

function onJobDialogClosed() {
  stopJobPolling()
}

function goOutline() {
  jobVisible.value = false
  router.push({ name: 'outline', params: { projectId } })
}

// ---------- 提取写作记忆 ----------
const extractingId = ref(null)

async function extractMemory(row) {
  try {
    await ElMessageBox.confirm(
      '将调用 AI 从该范文中总结角色、世界观、情节等关键事实，写入写作记忆库（需项目已生成大纲）。同源重复提取会归档旧记忆。',
      '提取写作记忆',
      { confirmButtonText: '开始提取', cancelButtonText: '取消', type: 'info' }
    )
  } catch {
    return
  }
  extractingId.value = row.source_id
  try {
    const { data } = await sourceApi.extractMemories(projectId, row.source_id)
    ElMessage.success(
      `记忆提取完成：新增 ${data.extracted || 0} 条` +
        (data.superseded ? `，归档旧记忆 ${data.superseded} 条` : '')
    )
  } catch {
    // 拦截器已提示
  } finally {
    extractingId.value = null
  }
}

async function onExtractFromJob() {
  const source = job.source
  jobVisible.value = false
  if (source) await extractMemory(source)
}

onMounted(() => {
  loadSources()
})

onUnmounted(() => {
  stopJobPolling()
  if (listTimer) {
    clearInterval(listTimer)
    listTimer = null
  }
})
</script>

<template>
  <div>
    <div class="page-title-row">
      <h2 class="page-title">范文管理</h2>
      <div class="actions">
        <el-button :icon="Refresh" @click="loadSources()">刷新</el-button>
        <el-button type="primary" :icon="UploadFilled" @click="openUpload">上传范文</el-button>
      </div>
    </div>

    <p class="page-tip">
      导入范文后依次完成：上传 → 解析分片 → AI 分析 → 提取写作记忆；之后即可在大纲页带着范文记忆生成大纲。
    </p>

    <div class="table-card">
      <el-table
        v-loading="loading"
        :data="sources"
        empty-text="还没有范文，点击右上角「上传范文」开始"
      >
        <el-table-column label="文件名" min-width="220">
          <template #default="{ row }">
            <span class="file-name">{{ row.filename }}</span>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ typeLabel(row.media_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ formatSize(row.byte_size) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tooltip
              v-if="row.failure_code"
              :content="`失败原因：${row.failure_code}`"
              placement="top"
            >
              <el-tag :type="statusMeta(row.status).type" size="small">
                {{ statusMeta(row.status).label }}
              </el-tag>
            </el-tooltip>
            <el-tag v-else :type="statusMeta(row.status).type" size="small">
              {{ statusMeta(row.status).label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="上传时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="250" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'accepted' || row.status === 'failed'"
              link
              type="primary"
              size="small"
              @click="startParse(row)"
            >
              开始解析
            </el-button>
            <el-button
              v-if="row.status === 'chunks_ready'"
              link
              type="primary"
              size="small"
              @click="startAnalysis(row)"
            >
              AI 分析
            </el-button>
            <el-button
              v-if="row.status === 'chunks_ready'"
              link
              type="success"
              size="small"
              :loading="extractingId === row.source_id"
              @click="extractMemory(row)"
            >
              提取记忆
            </el-button>
            <span v-if="row.status === 'parsing'" class="parsing-hint">解析中，自动刷新…</span>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 上传弹窗 -->
    <el-dialog
      v-model="uploadVisible"
      title="上传范文"
      width="560px"
      :close-on-click-modal="false"
    >
      <el-upload
        drag
        :auto-upload="false"
        :limit="1"
        accept=".txt,.docx,.pdf"
        :on-change="onFileChange"
        :on-remove="onFileRemove"
        class="upload-area"
      >
        <el-icon :size="36" color="#c0c4cc"><UploadFilled /></el-icon>
        <div class="upload-text">点击或拖拽文件到此处</div>
        <template #tip>
          <div class="upload-tip">支持 .txt / .docx / .pdf，单文件最大 10GB</div>
        </template>
      </el-upload>

      <el-divider content-position="left">版权与使用声明</el-divider>

      <el-form label-width="96px" label-position="left">
        <el-form-item label="权利基础">
          <el-radio-group v-model="authForm.basis">
            <el-radio v-for="opt in BASIS_OPTIONS" :key="opt.value" :value="opt.value">
              {{ opt.label }}
            </el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="requiredFields.rights_holder" label="权利持有人" required>
          <el-input
            v-model="authForm.rights_holder"
            placeholder="作品权利持有人名称"
            maxlength="255"
          />
        </el-form-item>

        <el-form-item v-if="requiredFields.license_identifier" label="许可标识" required>
          <el-input
            v-model="authForm.license_identifier"
            placeholder="如许可证编号 / 授权协议名称"
            maxlength="500"
          />
        </el-form-item>

        <el-form-item v-if="requiredFields.evidence_reference" label="来源证据" required>
          <el-input
            v-model="authForm.evidence_reference"
            placeholder="公有领域 / 明确许可的来源链接或说明"
            maxlength="500"
          />
        </el-form-item>

        <el-form-item label="允许用途">
          <el-checkbox-group v-model="permittedUses">
            <el-checkbox v-for="opt in USE_OPTIONS" :key="opt.value" :value="opt.value">
              {{ opt.label }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="doUpload">
          {{ uploading ? '上传中…' : '上传并解析' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 分析进度弹窗 -->
    <el-dialog
      v-model="jobVisible"
      title="范文 AI 分析"
      width="520px"
      :close-on-click-modal="false"
      @closed="onJobDialogClosed"
    >
      <div v-if="job.source" class="job-body">
        <p class="dialog-tip">正在分析：{{ job.source.filename }}</p>
        <el-progress
          :percentage="jobPercentage"
          :status="job.status === 'succeeded' ? 'success' : job.status === 'failed' ? 'exception' : undefined"
        />
        <div class="job-stats">
          <span>批次进度：{{ job.completed_batches }} / {{ job.total_batches }}</span>
          <span>状态：{{ JOB_STATUS_LABELS[job.status] || job.status }}</span>
          <span v-if="job.failed_batches">失败批次：{{ job.failed_batches }}</span>
        </div>
        <el-alert
          v-if="job.status === 'failed'"
          :title="`分析失败：${job.error_code || '未知错误'}`"
          type="error"
          :closable="false"
          show-icon
        />
        <div v-if="job.status === 'succeeded'" class="job-next">
          <el-button type="success" :icon="MagicStick" @click="onExtractFromJob">
            提取写作记忆
          </el-button>
          <el-button @click="goOutline">去生成大纲</el-button>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.page-title {
  margin: 0;
}
.actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
.page-tip {
  font-size: 13px;
  color: #909399;
  margin: 0 0 16px;
}
.table-card {
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 12px;
}
.file-name {
  font-weight: 500;
  color: #303133;
  word-break: break-all;
}
.parsing-hint {
  font-size: 12px;
  color: #909399;
}
.upload-area {
  width: 100%;
}
.upload-area :deep(.el-upload-dragger) {
  padding: 24px 0;
}
.upload-text {
  font-size: 14px;
  color: #606266;
  margin-top: 8px;
}
.upload-tip {
  font-size: 12px;
  color: #909399;
  margin-top: 6px;
}
.dialog-tip {
  font-size: 13px;
  color: #909399;
  margin: 0 0 12px;
}
.job-stats {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  font-size: 13px;
  color: #606266;
  margin: 12px 0;
}
.job-next {
  display: flex;
  gap: 8px;
  margin-top: 16px;
}
</style>
