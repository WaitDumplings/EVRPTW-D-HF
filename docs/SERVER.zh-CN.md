# 给服务器 agent 的任务

使用下列交接信息。仓库和提交号由发起人填写，每台机器分别指定城市、工作目录和分片。

| 交接项 | 示例 |
|---|---|
| Git 仓库与提交 | 仓库 URL、完整 commit SHA |
| 工作目录、城市 | `/data/evrptwd`、`chicago` |
| 来源锁 | 首机生成，其他机器读取同一份 `sources.lock.json` |
| 并发与分片 | 两个 worker。独占城市用 `0/1`，同城双机分别用 `0/2` 和 `1/2` |

## 可复制的 prompt

> 克隆指定仓库并检出指定提交。按 README 安装 Python 3.11 依赖和 osmium-tool。使用我指定的工作目录、城市和分片。直接从 SOURCES 中的发布机构下载原料，在本机裁剪和构造中间文件。启动各机时先同时运行 sources --only live，核对 live.lock.json 的 SHA-256，再把匹配的锁传给 sources --source-lock。默认道路快照为 2026-11-01。各机保留自己的 raw/ 缓存，此后可分时执行。先完成 11 城联合住宅户数校准，再为本机分配的城市构建并验证 Stage 1 CLE，执行 prepare-city 和 plan。记录 Git 提交、来源锁、城市计划的 SHA-256 和 Stage 1 日志。
>
> 本轮只验收 Chicago 的首个正式训练 Cus100。用 `stage2 --city chicago --split train --n 100 --limit 1 --workers 1` 生成，核对回执、恢复四个矩阵并绘图。对同一恢复目录执行 [Parquet 单例导出](PARQUET.zh-CN.md)，核对三种问题的 100 个客户及逐户需求，逐值比对所用的有向道路矩阵。回传 PNG、实例 ID、文件路径和核对结果后停止，等待确认。

每条命令和数据目录示例见[项目 README](../README.md)，输入列表和公开数据许可见[来源说明](SOURCES.zh-CN.md)。
