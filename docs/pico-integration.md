# Pico 代码执行引擎与持续协作

FaraFlow 后端继续使用本地 conda `zzx`。`native` 是默认代码引擎，Pico 通过独立进程接入；API 进程不导入 Pico，也不把 Pico 依赖安装进 `zzx`。

当前适配 Pico 0.1.7，固定源码提交为 `d6c7a648fd7ee63e472c0d438f1c8299d9b8f871`。上游要求 Python `>=3.12,<3.13`，因此 Python 3.8 的 `zzx` 可以运行 FaraFlow 后端及桥接测试，但不能充当 Pico Worker 的解释器。

## 配置

在独立 Python 3.12 环境中安装固定版本：

```powershell
& 'C:/path/to/pico-env/python.exe' -m pip install -r deploy/pico/requirements.txt
```

然后在 `.env` 中配置：

```dotenv
FARAFLOW_CODE_ENGINE=pico
FARAFLOW_PICO_PYTHON=C:/path/to/pico-env/python.exe
FARAFLOW_PICO_STATE_ROOT=./data/pico-runs
FARAFLOW_PICO_STARTUP_TIMEOUT_SECONDS=45
FARAFLOW_PICO_CONTEXT_WINDOW_TOKENS=32768
```

保留现有 `FARAFLOW_CODE_BASE_URL`、`FARAFLOW_CODE_MODEL` 和 `FARAFLOW_CODE_API_KEY`。`PICO_PYTHON` 必须是解释器的绝对路径。状态目录必须位于原项目、隔离副本和公开 `artifact_root` 之外。如果正在修改 FaraFlow 自身，应将 Pico 状态目录配置到 FaraFlow 项目目录之外。

`/health/ready` 的 `code_engine` 字段报告 Worker 是否能导入适配版本的 Pico，结果缓存 30 秒。模型端点仍独立检查。静态就绪不代表模型能够稳定使用原生 `tool_calls`，首次启用时应执行实际的小型修改任务。

切换回 `FARAFLOW_CODE_ENGINE=native` 并重启后端即可恢复原引擎。失败任务不会自动换引擎或重放。

## 运行边界

1. `CodeRuntime` 继续创建 worktree 或托管快照，并为单次 CodeRun 启动 Worker。
2. Worker 组装 Pico Scheduler、AgentTurnRunner 和 AgentLoop，只注册八个 FaraFlow 文件与 Git 查询代理工具。
3. Shell、spawn、Web、MCP、消息、定时任务和插件入口均不注册。
4. 工具请求回到宿主 `CodeToolExecutor`，继续执行路径、敏感文件、读后写、基线和审计检查。
5. 工具事件、引擎信息、最终回复和 token 用量通过现有事件接口推送；原始参数、结果正文和模型思考不直接作为公开事件转发。
6. 正常结束后仍生成统一 Diff 并等待人工审核，原目录只由既有 apply/revert 流程修改。

Worker 的工具限制是能力边界，不是操作系统沙箱。第一版没有任意命令、测试或构建执行能力。停止任务时，系统会等待在途文件操作和审计完成，再关闭 Worker 和清理隔离目录。

Pico 状态位于 `<PICO_STATE_ROOT>/<code_run_id>`，可能包含代码与模型会话，应按本地私有数据管理。它不会通过 artifacts API 发布，也不进入代码 Diff。

## 第二版持续协作

同一个 CodeRun 现在可以包含多个 Turn。初始要求和后续要求按顺序执行，共用隔离目录、基线、Session 和固定的引擎/模型配置。前端“继续修改”会追加 Turn；运行中追加的要求进入队列。

每轮结束后，系统重新对比初始基线并生成累计 Diff。发生文件变化时会创建不可变审核版本：

- `review_revision` 从 1 递增；
- 每个版本保存独立的文件快照、哈希与 Diff；
- 应用请求必须携带当前审核版本，过期版本返回 409；
- 应用使用审核快照，不读取可能继续变化的隔离目录；
- 应用和撤销按工作区串行执行，并继续检查原目录冲突。

“停止并保留修改”会取消当前及排队 Turn，等待正在进行的文件操作与审计完成，然后把任务置为 `PAUSED`。存在修改时仍可应用或继续修改。“丢弃任务”才会清理隔离目录。

后端重启会把执行中的 CodeRun 标记为 `INTERRUPTED`，保留隔离目录和历史记录；可以追加新 Turn 继续处理。当前版本尚未自动重放中断中的模型调用，也不开放测试命令、Shell、逐 token 输出或多 Agent 写入。

## 验证

后端验证使用现有 `zzx`：

```powershell
conda run -n zzx python -m pytest tests/test_code_workspace.py tests/test_pico_bridge.py tests/test_migrations.py tests/test_api.py tests/test_chat_service.py tests/test_chat_adapter.py -q
```

测试覆盖越界路径、敏感文件、读后写、多轮累计修改、轮次排队、暂停保留、审核版本冲突、不可变快照、撤销、畸形协议、启动超时和取消回收。它不能替代 Pico 与实际 CodingModel 的集成验收。

## 升级约束

适配层覆盖 Pico `_register_default_tools` 和 `_run_agent_loop` 两个内部接缝，因此必须固定源码版本。升级时要重新核对工具注册表、Turn 终态以及 Session/Context 初始化，再更新固定提交和契约测试。
