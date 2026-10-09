import axios from 'axios'
import { ElMessage } from 'element-plus'

/** 统一的 axios 实例：注入 Bearer Token，统一错误提示 */
const http = axios.create({
  baseURL: '/api/v1',
  timeout: 120000, // 生成类接口可能较慢
})

http.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

http.interceptors.response.use(
  (resp) => resp,
  (error) => {
    const status = error.response?.status
    if (status === 401) {
      localStorage.removeItem('token')
      if (!location.hash.includes('#/login')) {
        ElMessage.warning('登录已失效，请重新输入 Token')
        location.hash = '#/login'
      }
    } else {
      const msg =
        error.response?.data?.detail?.code ||
        error.response?.data?.detail ||
        (error.code === 'ECONNABORTED' ? '请求超时' : '网络错误')
      ElMessage.error(String(msg))
    }
    return Promise.reject(error)
  },
)

export default http