import http from './http'

/** 项目 API */
export const projectApi = {
  list: () => http.get('/projects'),
  create: (data) => http.post('/projects', data),
  get: (id) => http.get(`/projects/${id}`),
}

/** 大纲 API */
export const outlineApi = {
  generate: (projectId, targetChapters, userPrompt) =>
    http.post(`/projects/${projectId}/outline/generate`, {
      target_chapters: targetChapters,
      ...(userPrompt ? { user_prompt: userPrompt } : {}),
    }),
  get: (projectId) => http.get(`/projects/${projectId}/outline`),
  patch: (projectId, patch) => http.patch(`/projects/${projectId}/outline`, patch),
  listChapters: (projectId) => http.get(`/projects/${projectId}/outline/chapters`),
}

/** 章节 API */
export const chapterApi = {
  generate: (projectId, orderIndex) =>
    http.post(`/projects/${projectId}/chapters/${orderIndex}/generate`),
  get: (projectId, orderIndex) => http.get(`/projects/${projectId}/chapters/${orderIndex}`),
  list: (projectId) => http.get(`/projects/${projectId}/chapters`),
  patch: (projectId, orderIndex, patch) =>
    http.patch(`/projects/${projectId}/chapters/${orderIndex}`, patch),
}

/** 范文 API */
export const sourceApi = {
  initiateUpload: (projectId, data) =>
    http.post(`/projects/${projectId}/sources/uploads`, data),
  completeUpload: (projectId, uploadId, data) =>
    http.post(`/projects/${projectId}/sources/uploads/${uploadId}/complete`, data),
  list: (projectId, params) => http.get(`/projects/${projectId}/sources`, { params }),
  get: (projectId, sourceId) => http.get(`/projects/${projectId}/sources/${sourceId}`),
  startIngestion: (projectId, sourceId) =>
    http.post(`/projects/${projectId}/sources/${sourceId}/ingestions`),
  createAnalysisJob: (projectId, sourceId) =>
    http.post(`/projects/${projectId}/sources/${sourceId}/analysis-jobs`),
  getAnalysisJob: (projectId, sourceId, jobId) =>
    http.get(`/projects/${projectId}/sources/${sourceId}/analysis-jobs/${jobId}`),
  retryAnalysisJob: (projectId, sourceId, jobId) =>
    http.post(`/projects/${projectId}/sources/${sourceId}/analysis-jobs/${jobId}/retry`),
  extractMemories: (projectId, sourceId) =>
    http.post(`/projects/${projectId}/sources/${sourceId}/memories/extract`),
}

/** 写作记忆 API */
export const memoryApi = {
  extract: (projectId, chapterIndex) =>
    http.post(`/projects/${projectId}/memories/extract`, { chapter_index: chapterIndex }),
  list: (projectId, status) =>
    http.get(`/projects/${projectId}/memories`, { params: status ? { status } : {} }),
  get: (projectId, memoryId) => http.get(`/projects/${projectId}/memories/${memoryId}`),
  patch: (projectId, memoryId, patch) =>
    http.patch(`/projects/${projectId}/memories/${memoryId}`, patch),
}

/** 故事圣经/故事图 API（大纲页侧边栏展示用） */
export const bibleApi = {
  get: (projectId) => http.get(`/projects/${projectId}/story-bible`),
  style: (projectId) => http.get(`/projects/${projectId}/story-bible/style`),
}

export const graphApi = {
  get: (projectId) => http.get(`/projects/${projectId}/graph`),
}