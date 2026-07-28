# FaraFlow

FaraFlow 是一个面向企业场景的浏览器智能自动化 Agent 平台。它把 Microsoft
Fara1.5 放进受控的会话、审批、安全策略和审计边界中，而不是让模型直接拥有一台
不受限制的浏览器。

当前仓库实现的是可运行 MVP：

- Fara1.5-9B + vLLM OpenAI-compatible 推理适配；
- Playwright 1440×900 隔离 BrowserContext；
- Fara 官方 `computer_use` XML/JSON 工具协议与 1000×1000 坐标映射；
- 域名白名单、私网拦截、页面提示注入检测；
- 提交、购买、删除、发送和登录控件的模型外审批拦截；
- 任务、Session、Action、Approval、Event、Skill 持久化；
- 暂停、补充信息、一次性 resume token 与浏览器登录态 checkpoint；
- 截图、Playwright trace 和动作参数脱敏审计；
- FastAPI REST/WebSocket API 与 React 实时控制台；
- PostgreSQL、Redis、MinIO、Temporal 和 A6000/vLLM 部署基线。

## 系统架构

![FaraFlow 企业级浏览器智能自动化 Agent 平台系统架构图](./docs/imgs/FaraFlow%20—%20企业级浏览器智能自动化%20Agent%20平台（系统架构图）.png)

## 目录结构

```text
backend/faraflow/
  api/               FastAPI 与 WebSocket
  browser/           Playwright 隔离执行平面
  domain/            API/领域模型
  infra/             SQLAlchemy、工件与事件设施
  model/             Fara1.5 协议和 vLLM 适配器
  runtime/           Coordinator 任务状态机
  security/          allow-list、关键动作、注入检测
frontend/            React + Vite 控制台
deploy/              容器、安全与 A6000 部署文件
tests/               单元与 API 集成测试
```

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
