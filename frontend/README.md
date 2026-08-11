# Argus Job Search Console 前端

这是 Argus 的本地 dashboard。页面代码按职责拆分：

- `index.html`：页面骨架与可访问的表单控件
- `styles.css`：视觉系统、响应式布局、数据表与运行面板样式
- `table-filters.css`：职位、公司、地点列的紧凑表头筛选样式
- `top-navigation.css`：顶部横向页面导航布局
- `js/api.js`：只负责 HTTP API 请求
- `js/state.js`：页面状态与前端筛选逻辑
- `js/ui.js`：DOM 渲染、日志窗口、结果卡片和筛选控件
- `js/app.js`：应用启动、事件绑定和异步搜索轮询
- `../web/server.py`：静态文件服务、MySQL API、后台搜索任务

## 运行

在项目根目录运行：

```bash
./venv/bin/python web/server.py
```

打开 `http://127.0.0.1:8787`。如需修改端口：

```bash
ARGUS_PORT=9000 ./venv/bin/python web/server.py
```

## 行为说明

页面只从 MySQL 的 `jobs`、`companies` 与 `crawl_runs` 表读取职位和运行历史，不读取 `jobs.json`。职位结果是全局数据库视图，支持公司、地点、职位标题和申请状态筛选；列表按页加载。`applied` checkbox 会通过 API 直接更新数据库。

公司和地点筛选位于各自表头的多选下拉列表中，可搜索并勾选多个精确选项。职位标题筛选位于表头，按空格拆分关键词并执行包含匹配，例如 `software engineer` 会匹配同时含有两个词的职位。

“Runs & Logs”中的 profile 只用于决定 crawler 运行时读取哪套配置，不会划分或筛选数据库结果。crawler 仍保留既有的文件去重与 JSON/CSV 写入逻辑；只有新职位会写入 MySQL，并关联到该次 `crawl_runs` 记录。
