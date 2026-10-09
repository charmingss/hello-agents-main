---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '52d7cbe8-2cad-434b-9e7c-9832bde957dd'
  PropagateID: '52d7cbe8-2cad-434b-9e7c-9832bde957dd'
  ReservedCode1: 'd4064bef-9dfe-4892-97af-232ad7db373a'
  ReservedCode2: 'd4064bef-9dfe-4892-97af-232ad7db373a'
---

# AI 小说助手 Web 前端

基于 **Vue 3 + Vite + Element Plus** 的单页应用，提供大纲生成与章节创作的图形界面。

## 技术栈

- Vue 3（Composition API + `<script setup>`）
- Vite 8（构建）
- Element Plus（UI 组件库，按需自动导入）
- Pinia（状态管理，Token + 当前项目）
- Vue Router（Hash 路由）
- Axios（统一请求封装，自动注入 Bearer Token）

## 功能

| 页面 | 路由 | 说明 |
|------|------|------|
| 登录 | `#/login` | 输入 OIDC Token（仅存 localStorage） |
| 项目列表 | `#/projects` | 查看/创建项目，点击进入 |
| 大纲 | `#/projects/:id/outline` | 查看/生成/重新生成大纲，修改标题，章节卡片列表 |
| 章节 | `#/projects/:id/chapters` | 逐章生成正文、阅读、编辑（支持修订） |

## 开发

```bash
npm install        # 安装依赖
npm run dev        # 开发模式，端口 5173，/api 代理到 127.0.0.1:8000
```

开发时需同时启动后端（`make api` 或 `uvicorn novel_agent.main:app`）。

## 构建与部署

```bash
npm run build      # 产物输出到 web/dist
```

后端 `main.py` 启动时若检测到 `web/dist` 目录存在，会通过
`_mount_web_frontend()` 将其挂载到 `/`（同源访问，无需 CORS）。
访问 `http://127.0.0.1:8000/` 即打开前端界面。

挂载目录可通过配置项 `web_dist_dir` 覆盖（默认 `web/dist`）。

## 目录结构

```
web/
├── index.html
├── vite.config.js        # 代理 + 按需导入 + 构建配置
├── package.json
└── src/
    ├── main.js           # 应用入口
    ├── App.vue           # 全局布局（顶栏 + 导航）
    ├── style.css         # 全局样式（白底淡蓝）
    ├── router/index.js   # 路由 + 简单鉴权
    ├── stores/app.js     # Pinia：Token / 当前项目
    ├── api/
    │   ├── http.js       # Axios 封装（Token 注入、401 处理）
    │   └── index.js      # projectApi / outlineApi / chapterApi
    └── views/
        ├── LoginView.vue
        ├── ProjectsView.vue
        ├── OutlineView.vue
        └── ChaptersView.vue
```

## 说明

- Token 仅保存在浏览器 localStorage，请求时注入 `Authorization: Bearer`。
- 生成类接口超时设为 120 秒（LLM 生成耗时较长）。
- 阅读弹窗支持正文编辑，保存时携带 `expected_revision` 做乐观并发控制。
- 全部数据来自现有 FastAPI 业务 API，前端不持有任何业务数据。

> AI生成