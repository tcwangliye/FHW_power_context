# 忘川酒馆 · Lethe Tavern

用 Dify + PowerContext 做的跨会话记忆小游戏。

> 忘川河边有一间还亮着灯的酒馆。掌柜阿忘记性很好——记得你上次点了什么、怕不怕苦、有没有把酒泼在他身上。
> 但忘川的规矩是：**只要客人开口说"忘了吧"，他就真的忘掉。**

---

## 1. 作品与玩法

### 1.1 好玩在哪

玩家做的事本身很平常：聊天、点酒、恶作剧。乐趣来自**"他记得"和"他真的忘了"之间的反差**：

- 你说过的偏好，换个新会话他还记得，会照着做
- 你干的蠢事，他嘴上不饶人
- 你一句"忘了吧"，他从善如流——之后再提起那件事，他是真的想不起来了

"遗忘"在这里不是清空对话历史，而是**在持久记忆里把那条事实停用**，留下可审计的版本记录。这是别的东西做不到的部分。

### 1.2 怎么玩

| 步骤 | 你说 | 他做什么 |
| --- | --- | --- |
| 1 | `我叫小林，不吃香菜，酒要温的` | 接话，把这几点记住 |
| 2 | `我把你的酒杯打翻了，哈哈` | 不高兴，也记住了 |
| 3 | **新建对话** → `你还记得我吗？` | 主动提起小林、香菜、温酒、打翻酒杯 |
| 4 | `打翻你酒杯那件事，忘了吧` | 答应，把那件事停用 |
| 5 | **新建对话** → `我上次干过什么坏事吗？` | 想不起来打翻酒杯，其他照旧 |

完整步骤与判定见 [samples/scenario.md](samples/scenario.md)。

---

## 2. 技术分工

| 组件 | 版本 | 承担什么 |
| --- | --- | --- |
| **Dify**（Chatflow） | 1.19.0 / DSL 0.7.0 | 全部流程编排：接收输入、读取记忆、模型生成、解析记忆指令、条件分支、写回记忆、输出回答 |
| **PowerContext** | 1.2.0 | 持久记忆：按玩家分 Scope、跨会话保存、按查询召回、按玩家要求停用（不可变版本历史 + citation） |
| **bridge**（本项目配套后端） | 本仓库 `bridge/` | Dify 与 PowerContext 之间的适配层：玩家↔Scope 映射、记忆打包、自然语言遗忘目标匹配、错误归一 |

Dify 里的节点链：

```
开始 → 组装请求 → 读取记忆(HTTP) → 整理记忆(代码) → 阿忘(LLM) → 解析记忆指令(代码)
     → 要遗忘吗(IF) ─是→ 执行遗忘(HTTP) ┐
                    └─否───────────────┴→ 要记住吗(IF) ─是→ 写入记忆(HTTP) → 回答
                                                    └─否────────────────────→ 回答
```

模型只出现一次（`阿忘` 节点），所以单轮只有一次推理。

### 2.1 为什么用一个 bridge

Dify 可以直接调用 PowerContext 的 HTTP API，但有三件事在 DSL 里很难表达：

1. **玩家↔Scope 映射**：`POST /v1/scopes` 是按 `idempotency_key` 幂等的，需要把玩家标识稳定映射过去
2. **遗忘目标匹配**：玩家说"打翻酒杯那件事"，要落到具体某条记忆的 `citation` 才能停用
3. **citation 陈旧**：每次写入都会推进 Memory revision，之前取的 citation 立即失效，必须逐条重读

把这些放在 bridge 里，DSL 保持可读，也让这段逻辑能被单元测试覆盖。

### 2.2 为什么没有用 seekdb

题面要求 seekdb、PowerContext 二选一。这里选了 PowerContext，因为：

- 本作品的核心是**记忆的保存与遗忘**，这正是 PowerContext 的职责，seekdb 作为 Dify 数据库或向量库并不承载这个语义
- seekdb 的嵌入式绑定 `pylibseekdb` 不支持 Windows，而作者的演示环境是 Windows 11

两者同时使用需要在 Linux 上适配 Dify 的 seekdb 向量库分支，收益不抵成本。

---

## 3. 目录结构

```
dify/
├── README.md                      # 本文件
├── dsl/
│   └── lethe-tavern.chatflow.yml  # Chatflow 应用（唯一 DSL，无导入顺序问题）
├── bridge/                        # 配套后端（仅标准库，无第三方依赖）
│   ├── app.py                     # HTTP 服务与路由
│   ├── pc_client.py               # PowerContext HTTP 客户端
│   ├── tavern.py                  # 记忆打包、遗忘匹配、重置
│   ├── test_bridge.py             # 行为测试
│   └── Dockerfile
├── deploy/
│   ├── docker-compose.lethe.yaml  # 叠加到 Dify compose 的 override
│   ├── powercontext.Dockerfile    # PowerContext Server 镜像
│   ├── dify.env.additions         # 必须追加到 Dify .env 的配置
│   └── powercontext.env.example   # 主机开发模式的配置模板
├── scripts/
│   ├── verify_demo.py             # 端到端验证，产出证据
│   ├── reset_demo.py              # 恢复演示初始状态
│   └── check_dsl.py               # DSL 静态检查 + 代码节点执行检查
├── samples/
│   └── scenario.md                # 验证样例
└── evidence/
    └── run-*/scenario.json        # 实际运行记录
```

---

## 4. 版本与依赖

| 项 | 版本 | 说明 |
| --- | --- | --- |
| Dify | `1.19.0` | self-host，Docker Compose 部署 |
| Dify DSL | `0.7.0` | 1.19.0 的 `CURRENT_APP_DSL_VERSION` |
| Docker Compose | `>= 2.35.0` | Dify 1.19.0 的 `envs/` 分层配置依赖该版本 |
| Dify 插件 | `langgenius/openai_api_compatible:0.0.84` | 已在 DSL `dependencies` 中声明，导入时会提示安装 |
| PowerContext | `1.2.0` | `powercontext[cli,server]`，SQLite 后端 |
| bridge Python | `3.12` | 只用标准库 |

**资源**：Dify 官方要求 2 核 / 4 GiB；叠加 PowerContext 与 bridge 后实测 4 核 / 8 GiB 较宽裕。无需 GPU。

---

## 5. 从零跑到能玩

### 5.1 准备 Docker

Windows 需要 WSL2 + Docker Desktop（安装时勾选 *Use WSL 2 based engine*）。Win11 家庭版即可，不需要专业版 Hyper-V。

```powershell
wsl --install          # 管理员 PowerShell，装完重启
```

装好 Docker Desktop 后确认版本：

```bash
docker compose version   # 需要 >= 2.35.0
docker run --rm hello-world
```

### 5.2 取 Dify 1.19.0

```bash
git clone --branch 1.19.0 --depth 1 https://github.com/langgenius/dify.git /path/to/dify
cd /path/to/dify/docker
cp .env.example .env
```

按 Dify 1.19.0 的要求补齐两项必填：

```dotenv
# 生成：openssl rand -hex 32
DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN=<你的随机串>
COMPOSE_PROFILES=weaviate,postgresql,collaboration
```

**然后追加 SSRF 放行配置**（见 [deploy/dify.env.additions](deploy/dify.env.additions)）：

```dotenv
SSRF_PROXY_ALLOW_PRIVATE_IPS=172.28.0.0/16
```

> Dify 的 HTTP Request 节点、工具调用和模型调用都会走内置的 ssrf_proxy，默认**拒绝私有地址**。
> 不放行这一段，所有 HTTP 节点都会报 `blocked by SSRF protection`。
> 这段 CIDR 与 `deploy/docker-compose.lethe.yaml` 里固定的子网一致。

### 5.3 启动 Dify + PowerContext + bridge

在 Dify 的 `docker/` 目录下，用本仓库的 override 叠加启动：

```bash
cd /path/to/dify/docker
docker compose \
  -f docker-compose.yaml \
  -f /path/to/this-repo/dify/deploy/docker-compose.lethe.yaml \
  up -d
```

确认服务就绪：

```bash
docker compose ps
docker compose logs -f lethe-bridge      # 出现 bridge listening on 0.0.0.0:8080 即可
curl -s http://127.0.0.1:18000/health/ready
curl -s http://127.0.0.1:8080/healthz
```

`/healthz` 返回 `{"status":"ok","powercontext":"ready"}` 表示三层已连通。

PowerContext 的数据在 named volume `lethe-powercontext-data` 里；`18000` 只绑定 loopback，供检查用。

### 5.4 建管理员并配模型

1. 打开 `http://localhost/install`，创建管理员账号
2. 进入右上角 **设置 → 模型供应商**，安装 **OpenAI-API-compatible**
   （DSL 里已声明该依赖，导入时也会提示安装）
3. 在该供应商下 **添加模型**：

| 字段 | 填什么 |
| --- | --- |
| 模型类型 | LLM |
| 模型名称 | 建议填 `lethe-npc`，让 DSL 直接可用 |
| API Key | `<你的 Key>`（本地 Ollama 可随意填） |
| Base URL | 你的 OpenAI 兼容端点，例如 `https://api.deepseek.com/v1` |
| 上下文长度 / 最大 token | 8192 / 2048 即可 |

> 如果你的网关不允许自定义模型名（名字必须等于上游真实模型 id），导入后打开 **阿忘** 节点，
> 在模型下拉框里重新选一次即可。DSL 里默认写的是 `lethe-npc`。

4. 也可以用 **Ollama** 完全离线：`ollama pull qwen2.5:7b`，Base URL 填
   `http://host.docker.internal:11434/v1`，模型名 `qwen2.5:7b`，API Key 随便填。
   此时需要把 SSRF 放行范围扩大到你所在网段，例如
   `SSRF_PROXY_ALLOW_PRIVATE_IPS=172.28.0.0/16,192.168.0.0/16`。

### 5.5 导入 DSL

**工作室 → 创建空白应用 → 导入 DSL 文件**，选择 [dsl/lethe-tavern.chatflow.yml](dsl/lethe-tavern.chatflow.yml)。

只有一个 DSL 文件，导入顺序无要求。导入后如果提示插件未安装，点安装即可。

### 5.6 应用入口

两种，任选：

- **网页对话**：应用页面直接聊天。同一浏览器内 `user` 标识自动保持一致，新建对话即"换了场合但人还是你"。
- **服务 API**：在应用页左侧 **访问 API** 里创建密钥，然后：

```bash
curl -X POST 'http://localhost/v1/chat-messages' \
  -H 'Authorization: Bearer app-xxxxxxxxxxxx' \
  -H 'Content-Type: application/json' \
  -d '{"inputs":{},"query":"我叫小林，不吃香菜","response_mode":"blocking","user":"demo-lin"}'
```

---

## 6. 验证

```bash
export DIFY_APP_KEY=app-xxxxxxxxxxxx
python scripts/verify_demo.py --mode dify --player demo-lin
```

脚本按 [samples/scenario.md](samples/scenario.md) 跑完五步，把每一步的请求、回答、记忆快照和判定写入
`evidence/run-<时间戳>/scenario.json`，并打印 PASS/FAIL 摘要。

同时抓取另外两类记录：

```bash
docker compose logs lethe-bridge > evidence/bridge.log
docker compose logs lethe-powercontext > evidence/powercontext.log
```

没有 Dify 时可以只验证 PowerContext 链路：

```bash
python scripts/verify_demo.py --mode bridge --player demo-lin
```

已有的运行记录和"还需要补什么"见 [evidence/README.md](evidence/README.md)。

恢复初始状态：

```bash
python scripts/reset_demo.py --player demo-lin
```

---

## 7. 主机开发模式（不用 Docker 跑后端）

适合调 bridge 或看 PowerContext 行为：

```bash
# 1. PowerContext
uv tool install --force "powercontext[cli,server]==1.2.0"
powercontext server run          # 127.0.0.1:8000

# 2. bridge
cd dify/bridge
POWERCONTEXT_URL=http://127.0.0.1:8000 python app.py    # 0.0.0.0:8080

# 3. 跑测试与场景
cd dify/bridge && POWERCONTEXT_URL=http://127.0.0.1:8000 python -m unittest test_bridge -v
cd dify && python scripts/verify_demo.py --mode bridge
```

Docker 里的 Dify 要访问主机上的 bridge 时，把 DSL 里的 `http://lethe-bridge:8080` 改成
`http://host.docker.internal:8080`，并相应放行 SSRF。

---

## 8. bridge 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/livez` | 进程存活 |
| GET | `/healthz` | 进程 + PowerContext 就绪状态 |
| POST | `/turn/context` | `{player_id, utterance}` → 该玩家的记忆块与命中条目 |
| POST | `/turn/remember` | `{player_id, memories:[{kind,text,reason}]}` → 写入结果 |
| POST | `/turn/forget` | `{player_id, targets:[...]}` → 停用结果与未匹配目标 |
| GET | `/players/{id}/memories` | 记忆清单（`?include_inactive=true` 含已停用） |
| POST | `/players/{id}/reset` | 停用该玩家全部活跃记忆 |

错误统一为 `{"error":{"code":"...","message":"..."}}`；上游冲突映射为 409，上游不可达映射为 502。

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `POWERCONTEXT_URL` | `http://127.0.0.1:8000` | PowerContext 地址 |
| `POWERCONTEXT_TOKEN` | 空 | 启用鉴权时填 |
| `LETHE_MEMORY_LIMIT` | `24` | 每轮最多带多少条记忆 |
| `LETHE_MEMORY_CHARS` | `4000` | 记忆块字符上限 |
| `LETHE_SCOPE_PREFIX` | `lethe-tavern` | Scope 幂等键前缀 |
| `LETHE_BRIDGE_TOKEN` | 空 | 设置后要求 `Authorization: Bearer` |

---

## 9. 已知限制

1. **遗忘匹配是词面的**。bridge 先用 PowerContext 的全文检索，再退回字符二元组相似度。
   如果玩家用完全不同的词描述同一件事（"那件让我很没面子的事"），可能匹配不到。
   此时接口会返回 `unmatched`，阿忘会照实说不记得——不会凭空删错条目。
2. **新会话记忆靠玩家标识**。Dify 网页版的 `user` 存在浏览器本地存储里：同一浏览器 = 同一个人，
   清缓存或换浏览器就是新人。要严格复现请用服务 API 显式传 `user`。
3. **记忆列表是全量读取**。Scope 即玩家，因此每轮读取该玩家全部活跃记忆（默认上限 24 条）。
   这是刻意的取舍：酒馆场景每人几十条，换来"换会话也记得你"这个核心体验。
   玩家记忆量非常大时应改为分页 + 检索优先。
4. **单次模型调用**。阿忘的台词和记忆指令由同一次推理产出，用 `<<<MEMORY>>>` 分隔。
   小模型偶尔会漏掉这段，此时这一轮不写入记忆，但对话照常——不会因此报错。
5. **未配置推理服务时 PowerContext 仍可用**。本项目只用显式记忆写入，不依赖自动抽取和向量检索，
   因此不需要给 PowerContext 配模型。

---

## 10. 数据来源与许可

- 本项目所有代码与文档为原创，遵守仓库根目录 `LICENSE`。
- Dify 采用其自身许可（Apache-2.0 附加条件），本仓库不分发 Dify 源码或镜像。
- PowerContext 来自本仓库（OceanBase，Apache-2.0）。
- 没有复用第三方公开 DSL；`dsl/` 下的工作流按 Dify 0.7.0 结构自行编写。
- 样例中的玩家名、酒馆设定均为虚构，不含真实个人信息。
