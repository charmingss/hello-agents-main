import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/projects' },
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/LoginView.vue'),
    meta: { public: true },
  },
  {
    path: '/projects',
    name: 'projects',
    component: () => import('../views/ProjectsView.vue'),
  },
  {
    path: '/projects/:projectId/sources',
    name: 'sources',
    component: () => import('../views/SourcesView.vue'),
  },
  {
    path: '/projects/:projectId/outline',
    name: 'outline',
    component: () => import('../views/OutlineView.vue'),
  },
  {
    path: '/projects/:projectId/chapters',
    name: 'chapters',
    component: () => import('../views/ChaptersView.vue'),
  },
  {
    path: '/projects/:projectId/memories',
    name: 'memories',
    component: () => import('../views/MemoriesView.vue'),
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

// 简单鉴权：未登录时跳转登录页
router.beforeEach((to) => {
  const token = localStorage.getItem('token')
  if (!to.meta.public && !token) {
    return { name: 'login' }
  }
  return true
})

export default router