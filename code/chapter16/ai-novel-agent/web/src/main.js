import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'
import './style.css'

// Element Plus 组件由 unplugin-vue-components 按需自动导入；
// ElMessage/ElMessageBox 等命令式 API 在组件内手动 import。
const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')