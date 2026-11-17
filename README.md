# 分布式网页抓取调度平台

面向分布式网页抓取任务调度、节点协同、请求分配、反爬策略编排、数据校验入库与抓取质量大盘的采集工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集节点 | `worker` | 工作节点 | 节点编号、主机地址、并发槽位数 |
| 请求分配 | `dispatch` | 分配策略 | 策略编号、目标域名、分配算法 |
| 抓取作业 | `crawljob` | 作业定义 | 作业编号、目标站点、起始URL |
| URL过滤 | `urlfilter` | 过滤规则 / 规则快照 | 规则编号、包含模式、排除模式、域名白名单、路径黑名单、参数剥离规则、候选URL |
| DNS解析 | `dnsresolver` | 解析配置 | 配置编号、域名模式、DNS服务器 |
| 内容提取 | `contentextract` | 提取器 | 提取器编号、内容类型、正文选择器 |
| 存储策略 | `storagestrategy` | 存储方案 | 方案编号、存储类型、连接参数 |
| 渲染任务 | `rendertask` | 渲染作业 | 作业编号、目标URL、浏览器版本 |
| 采集质量 | `qualityreport` | 质量报告 | 报告编号、作业编号、总URL数 |
| 带宽控制 | `bandwidth` | 带宽策略 | 策略编号、节点标识、下载带宽上限 |
| 重试队列 | `retryqueue` | 重试条目 | 条目编号、失败URL、失败原因码 |
| 翻页管理 | `pagemarker` | 翻页规则 | 规则编号、目标站点、翻页类型 |
| 增量标记 | `incrementmark` | 增量水位 | 水位编号、目标源、增量字段名 |
| 配置同步 | `configsync` | 同步任务 | 任务编号、源节点、目标节点 |
| 采集API | `scrapeapi` | API定义 | API编号、API名称、请求路径 |
| 数据抽样 | `datasample` | 抽样方案 | 方案编号、数据源、抽样方式 |
| 采集告警 | `alertpolicy` | 告警策略 | 策略编号、监控指标、触发条件 |

## URL过滤与URL准入

URL过滤模块把“规则保存 → 确定命中 → 快照 → 页面落地”串成一条可复核的链路：

- **完整字段保存**：登记/编辑接口一次校验并保存规则编号、包含/排除模式、域名白名单、
  路径黑名单、参数剥离规则、大小写处理、尾部斜杠策略、候选URL、匹配范围、规则上限、
  冲突策略。任一字段非法都不会产生“部分字段已保存”的中间状态，错误按字段+下标返回。
- **确定口径**：`GET /api/urlfilter/policy` 返回固定口径（上限、匹配范围、冲突条件、
  非法值标准），页面上的“查看规则口径”与接口共用同一份定义。
- **唯一命中**：同一URL同时命中放行条件（白名单/包含模式）与拒绝条件（路径黑名单/排除模式）
  时按“拒绝优先”收敛，结果唯一；规范化参数（大小写、尾部斜杠、参数剥离）在命中前生效，
  每条结果都带规范化说明。
- **规则快照**：规则生效时生成不可变快照，保存完整规则字段、候选URL和去重后的URL明文结果；
  同一规则/版本/内容/候选URL只允许一份快照（幂等键），并发或重复提交都不会生成第二份范围。
- **换版不影响历史**：新版本只新增快照，旧快照与它的去重URL清单保留原命中记录，可继续
  `POST /api/urlfilter/snapshots/{id}/verify` 逐项复核。
- **落地与作业**：快照同步到页面落地侧清单（渲染任务，幂等、有同步记录）；抓取作业启动时必须
  解析到一份快照，违规URL只进入拦截清单，不进入作业执行参数。

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/urlfilter/policy` | 规则口径 |
| POST | `/api/urlfilter` / PUT `/api/urlfilter/{id}` | 登记 / 整条覆盖保存（带 version 防并发覆盖） |
| POST | `/api/urlfilter/admission` | 按规则或快照计算候选URL的唯一准入结果 |
| GET | `/api/urlfilter/snapshots` | 快照列表 |
| GET | `/api/urlfilter/snapshots/{id}` | 快照详情（规则字段、去重URL、同步记录） |
| GET | `/api/urlfilter/snapshots/{id}/urls` | 去重URL清单，可按准入/拒绝筛选 |
| POST | `/api/urlfilter/snapshots/{id}/verify` | 按快照规则逐项复算，确认无命中错位 |
| POST | `/api/urlfilter/snapshots/{id}/sync` | 重新同步到页面落地侧（幂等） |
| GET | `/api/urlfilter/sync-records` | 同步记录 |
| GET | `/api/rendertask/snapshots/{id}` | 页面落地侧清单核对 |
| POST | `/api/crawljob/{id}/actions` | 启动作业时生成只含准入URL的执行参数 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
