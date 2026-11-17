# 运行证据

本目录记录实际运行结果，供评审核对。

## 1. `run-20261117-220241/` —— Dify 端完整运行

**模式**：`verify_demo.py --mode dify`（经 Dify 服务 API 驱动真实 Chatflow）

**环境**

| 项 | 值 |
| --- | --- |
| 主机 | Windows 11、Docker Desktop 29.9.3、Docker Compose v5.7.0 |
| Dify | `1.19.0`（self-host，`docker compose`） |
| DSL | `0.7.0`，11 节点，导入状态 `completed`，无 error / 无 warning |
| PowerContext | `1.2.0`（容器内，SQLite 后端） |
| bridge | 本仓库 `bridge/`（容器内，`172.28.0.x`） |
| 模型 | **脚本替身**（见下方说明） |

**产物**

| 文件 | 内容 |
| --- | --- |
| `scenario.json` | 五步场景的请求、回答、记忆快照与判定（7 步，0 失败） |
| `bridge.log` | bridge 收到的每次 `/turn/*` 调用与耗时 |
| `powercontext.log` | PowerContext 的 transport 请求记录 |
| `dify-api.log` | Dify API 侧的节点/插件调用摘要 |
| `01-workspace.png` | Dify 工作室中的应用列表 |
| `02-workflow-graph.png` | 编排画布：11 节点与全部连线，状态"已发布" |
| `03-logs.png` | Dify 运行日志：5 次对话全部 `SUCCESS` |
| `04-chat.png` | 网页应用对话界面与开场白 |
| `05-node-execution.png` | 单次运行详情（耗时、Token） |

**判定结果**

| 步骤 | 检查项 | 结果 |
| --- | --- | --- |
| 1 | 对话后至少写入一条记忆 | PASS |
| 1 | 记住了「不吃香菜」 | PASS |
| 2 | 记住了「打翻酒杯」这件事 | PASS |
| 3 | 全新会话里阿忘提到旧事 | PASS |
| 4 | 停用 1 条记忆 | PASS |
| 4 | 「打翻酒杯」已不在活跃记忆中 | PASS |
| 5 | 遗忘后不再提起 | PASS |
| 5 | 其余记忆仍然保留 | PASS |

## 关于模型的诚实说明

本机没有模型凭据，安装本地推理运行时也未获授权。因此这次运行使用
`deploy/model_stub/` 里的 **脚本替身**：一个实现 OpenAI chat-completions 协议的确定性 HTTP 服务，
按玩家输入返回符合 `<<<MEMORY>>>` 协议的台词。

它验证的是 **Dify 编排链路**：节点数据流、HTTP 请求体构造、IF 分支判定、记忆写入与停用。
它 **不** 验证模型的话术质量。换成真实模型只需在 Dify 里把模型指向任意 OpenAI 兼容端点，
DSL 与 bridge 都不用改。

`README.md` 第 5.4 节给出了换成 DeepSeek / 通义 / Ollama 的步骤。

## 2. `dryrun-*/` —— 不依赖 Dify 的编排链路验证

`dryrun-remember/` 与 `dryrun-forget/` 用 DSL 里**真实的代码节点**跑通整条链路，只把 LLM 换成脚本台词。
它证明节点之间的数据流和分支判定正确，可在没有 Dify 的环境里复现：

```bash
python scripts/dryrun_flow.py --player demo-lin --bridge http://127.0.0.1:8080
```

## 3. `run-20261117-201204/` —— 仅 PowerContext 链路

早期记录，`verify_demo.py --mode bridge`，不经过 Dify。
保留它是因为它证明了 `bridge → PowerContext` 的契约在 Dify 之外同样成立。

## DSL 的两道校验

| 工具 | 覆盖 | 结果 |
| --- | --- | --- |
| `scripts/check_dsl.py` | 节点连通性、变量引用、代码节点实际执行 | 通过 |
| `scripts/validate_with_dify.py` | Dify 1.19.0 自己的导入器（`graphon.dsl.loads`）逐节点 schema | 11 节点全部通过 |

两道静态校验都没能发现的一个真实缺陷，是这次真实运行暴露出来的：

> `npc_node` 的 `prompt_template` 只有 `system` 消息，没有 `user` 消息，模型收不到玩家说的话。
> 表现是每轮都返回兜底台词、记忆里一条也写不进去。

已修复（增加 `- role: user` / `text: '{{#sys.query#}}'`），修复后重新导入、发布，五步全部通过。
这说明静态校验不能替代一次真实运行。
