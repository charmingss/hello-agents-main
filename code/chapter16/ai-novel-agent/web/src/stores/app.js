import { defineStore } from 'pinia'
import { ref } from 'vue'

/** 全局状态：Token 与当前选中的项目 */
export const useAppStore = defineStore('app', () => {
  const token = ref(localStorage.getItem('token') || '')
  const currentProject = ref(
    JSON.parse(localStorage.getItem('currentProject') || 'null'),
  )

  function setToken(value) {
    token.value = value
    if (value) {
      localStorage.setItem('token', value)
    } else {
      localStorage.removeItem('token')
    }
  }

  function setCurrentProject(project) {
    currentProject.value = project
    if (project) {
      localStorage.setItem('currentProject', JSON.stringify(project))
    } else {
      localStorage.removeItem('currentProject')
    }
  }

  return { token, currentProject, setToken, setCurrentProject }
})