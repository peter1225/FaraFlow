# FaraFlow

FaraFlow 是一个面向企业场景的浏览器智能自动化 Agent 平台。它把 Microsoft
Fara1.5 放进受控的会话、审批、安全策略和审计边界中，而不是让模型直接拥有一台
不受限制的浏览器。

当前仓库实现的是可运行 MVP，已经包含以下框架：

- Fara1.5-9B + vLLM OpenAI-compatible 推理适配，可连接本机或远程模型服务器；
- React 19 + Vite 对话工作区，支持普通问答与自动化请求的统一入口；
- `ChatService` 意图路由：无需实时网页信息时直接对话，需要搜索或操作网页时自动创建浏览器任务；
- 自动化任务控制台：任务计划、浏览器截图、执行时间线、审批提示和运行状态；
- 深色/浅色主题切换，并将用户选择保存到浏览器本地存储；
- `AgentRuntime` 浏览器 Agent 主循环：截图 → 模型决策 → 安全检查 → Playwright 执行 → 结果回传；
- Playwright 1440×900 隔离 BrowserContext 与并发会话池；
- Fara 官方 `computer_use` XML/JSON 工具协议与 1000×1000 坐标映射；
- 域名白名单、私网拦截、页面提示注入检测；
- 提交、购买、删除、发送和登录控件的模型外审批拦截；
- 任务、Session、Action、Approval、Event、Skill 持久化；
- 暂停、补充信息、一次性 resume token 与浏览器登录态 checkpoint；
- 截图、Playwright trace 和动作参数脱敏审计；
- FastAPI REST/WebSocket API 与 React 实时控制台；
- PostgreSQL、Redis、MinIO、Temporal 和 A6000/vLLM 部署基线。

## 系统架构

![FaraFlow 企业级浏览器智能自动化 Agent 平台系统架构图](<./docs/imgs/FaraFlow — 企业级浏览器智能自动化 Agent 平台（系统架构图）-v2.png>)

## 目录结构

```text
backend/faraflow/
  api/               FastAPI 路由、生命周期和 WebSocket 事件接口
  browser/           BrowserPool、Playwright 隔离执行平面
  domain/            API 请求/响应模型与任务状态枚举
  infra/             SQLAlchemy、Repository、工件与事件设施
  model/             Fara1.5 协议解析和 vLLM 适配器
  runtime/           ChatService、TaskService、AgentRuntime
  security/          域名白名单、关键动作审批、注入检测
frontend/
  src/App.tsx        对话、任务、审批和主题切换界面
  src/api.ts         REST 与 WebSocket 客户端
  src/types.ts       前端领域类型
  src/styles.css     控制台视觉主题
data/                本地 SQLite 数据库（开发运行时生成）
artifacts/           截图与 Playwright trace（开发运行时生成）
browser-state/       浏览器登录态 checkpoint（开发运行时生成）
deploy/              容器、安全与 A6000 部署文件
tests/               单元、API、浏览器和 Agent 运行时测试
```

## 当前运行链路

用户从控制台发送消息后，后端先由 `ChatService` 判断请求类型：

1. 普通问答、解释或写作请求直接调用 `FaraAdapter`，通过 OpenAI 兼容接口向 vLLM 请求回复，并将消息保存到 SQLite。
2. 搜索、打开、访问或查询实时网页时，系统创建自动化任务并交给 `AgentRuntime`。
3. `AgentRuntime` 为任务创建隔离浏览器会话，循环执行“截图 → Fara 决策 → 安全策略 → Playwright 动作”。
4. 每个动作会写入 Action/Event 记录；截图和 Playwright trace 保存到 `artifacts/`，浏览器登录态保存到 `browser-state/`。
5. 任务完成、暂停、等待用户输入、等待审批、触发安全接管或失败时，状态通过 REST 和 WebSocket 同步到前端。

当前开发环境可以把 `.env` 中的模型配置指向远程 vLLM OpenAI API，例如：

```dotenv
FARAFLOW_FARA_BASE_URL=http://127.0.0.1:5000/v1
FARAFLOW_FARA_MODEL=microsoft/Fara1.5-9B
FARAFLOW_FARA_API_KEY=not-needed
```

如果模型部署在内网服务器，将 `FARAFLOW_FARA_BASE_URL` 改成该服务器的 `/v1` 地址即可，前端和后端不需要修改模型调用代码。

## 本地开发

控制面支持 Python 3.8+（生产建议 3.11+），前端要求 Node.js 20+。模型服务器建议
运行在 Linux GPU 主机；Windows 可直接使用现有 Conda 环境开发。

使用 Conda：

```powershell
conda create -n faraflow python=3.11 -y
conda activate faraflow
Copy-Item .env.example .env
python -m pip install -e ".[dev]"
python -m playwright install chromium
uvicorn faraflow.api.main:app --host 127.0.0.1 --port 8080
```

Linux/WSL2 也可以使用独立虚拟环境：

```bash
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
uvicorn faraflow.api.main:app --host 0.0.0.0 --port 8080
```

另开终端启动控制台：

```bash
cd frontend
npm install
npm run dev
```

访问 `http://localhost:5173`；OpenAPI 文档位于
`http://localhost:8080/docs`。

默认使用 SQLite，适合本地开发。将 `FARAFLOW_DATABASE_URL` 改为
`postgresql+asyncpg://...` 即可切换 PostgreSQL。

当前 MVP 的任务循环在 API 进程内运行；Compose 已包含 Temporal 服务，供生产阶段
将同一状态机迁移为 durable workflow。单机部署应先用单个 API worker，避免重复执行。

主要 API 包括：

- `POST /v1/chats`、`POST /v1/chats/{chat_id}/messages`：创建对话并发送消息；
- `POST /v1/tasks`：直接创建自动化任务；
- `POST /v1/tasks/{task_id}/start|pause|terminate|respond`：控制任务生命周期；
- `POST /v1/tasks/{task_id}/approvals/{approval_id}`：处理高风险动作审批；
- `WS /v1/sessions/{session_id}/events`：接收实时执行事件；
- `GET /v1/artifacts/{artifact_path}`：查看任务截图和 trace 工件。

## A6000 一键部署

部署机需要：

- Ubuntu 22.04/24.04；
- NVIDIA RTX A6000 48GB 与可用驱动；
- Docker Engine、Compose plugin、NVIDIA Container Toolkit；
- 至少 150GB 可用磁盘用于镜像、模型权重与工件。

```bash
cp .env.example .env
./deploy/a6000/preflight.sh
docker compose -f docker-compose.yml -f docker-compose.a6000.yml up -d --build
```

上线前先修改 `.env` 中的 PostgreSQL/MinIO 口令；若 Hugging Face 下载需要鉴权，
再填入 `HF_TOKEN`。首次启动会下载 `microsoft/Fara1.5-9B`。查看状态：

```bash
docker compose -f docker-compose.yml -f docker-compose.a6000.yml ps
docker compose -f docker-compose.yml -f docker-compose.a6000.yml logs -f vllm
curl http://localhost:5000/v1/models
curl http://localhost:8080/health/ready
```

默认 `VLLM_MAX_MODEL_LEN=65536`，这是单张 A6000 上更保守的生产起点。完成显存与
长轨迹压测后可以逐步提高到模型支持的 262144。FaraFlow 每次只保留最近三张截图，
通常无需为普通浏览器任务直接打开完整上下文。

## 最小 API 示例

```bash
curl -X POST http://localhost:8080/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "task_name": "官网更新检索",
    "description": "查找 Microsoft Fara1.5 的主要能力并给出简短总结，不要登录或提交表单",
    "start_url": "https://www.bing.com/",
    "allowed_domains": ["bing.com", "microsoft.com", "github.com", "huggingface.co"]
  }'
```

使用响应中的 `task_id` 启动：

```bash
curl -X POST http://localhost:8080/v1/tasks/TASK_ID/start
```

## 安全说明

- 不要把真实凭证写进任务描述、`.env` 示例、日志或 Skill manifest。
- 生产环境应在 API 前加入 OIDC/RBAC，并把 resume token 绑定当前用户和租户。
- 浏览器层 allow-list 之外，还应使用 NetworkPolicy/egress proxy 做第二层限制。
- 当前提示注入检测采取安全优先策略，命中后进入 `HANDOFF`；需人工检查页面。
- Fara1.5 是研究预览模型。高风险领域和生产流程必须先做场景回归与人工监督。

模型与参考实现：
[Fara1.5 模型集合](https://huggingface.co/collections/microsoft/fara15)、
[Microsoft Fara 官方仓库](https://github.com/microsoft/fara)。
