# 运行证据

## 已包含：`run-20261117-201204/`

**模式**：`verify_demo.py --mode bridge`
**环境**：Windows 11、Python 3.12.7、PowerContext 1.2.0（本机 Server，SQLite 后端）、bridge 以主机进程运行
**时间**：2026-11-17T20:12+08:00

这条记录证明的是 **Dify ↔ bridge ↔ PowerContext 之间的契约**：记忆写入、跨会话读取、停用、停用后不再召回。
它不经过 Dify 的流程编排，所以不能单独作为"应用能在 Dify 里跑起来"的证据。

`scenario.json` 里每一步都记了 `scope_id`、记忆条目 `entry_id`、`kind` 和原文，对应 PowerContext 中真实的
Artifact revision。

判定结果：

| 步骤 | 检查项 | 结果 |
| --- | --- | --- |
| 1 | 对话后至少写入一条记忆 | PASS |
| 1 | 记住了「不吃香菜」 | PASS |
| 2 | 记住了「打翻酒杯」这件事 | PASS |
| 3 | 全新会话仍能读到旧事 | PASS |
| 4 | 停用 1 条记忆 | PASS |
| 4 | 「打翻酒杯」已不在活跃记忆中 | PASS |
| 5 | 其余记忆仍然保留 | PASS |

## 待补：Dify 端的运行证据

题面 4.3 还要求"实际运行结果"包含 Dify 侧的流程编排与模型输出。补齐步骤：

1. 按 [../README.md](../README.md) 第 5 节启动 Dify 1.19.0 + PowerContext + bridge
2. 导入 `dsl/lethe-tavern.chatflow.yml`，配置模型
3. 在应用页创建 API Key
4. 执行：

```bash
export DIFY_APP_KEY=app-xxxxxxxxxxxx
python scripts/verify_demo.py --mode dify --player demo-lin
docker compose logs lethe-bridge > evidence/bridge.log
docker compose logs lethe-powercontext > evidence/powercontext.log
```

5. 截图：Dify 应用对话页（步骤 3 的新会话回答、步骤 5 遗忘后的回答）、应用的节点执行记录

补齐后请把产物放在 `evidence/run-<时间戳>/` 下，并在本节记录环境与版本。

> 说明：本仓库作者环境没有 Docker，故 Dify 侧运行未能在此完成。
> DSL 已通过 `scripts/check_dsl.py` 的静态检查（节点连通性、变量引用、代码节点执行），
> 该检查能发现导入后才会暴露的大部分结构性错误，但不代替一次真实导入。
