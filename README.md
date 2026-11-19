# EVRPTW-D

从公开原始数据构造美国 11 城的电动货车配送实例。仓库提供 Python 代码、拟合后的模型参数、来源清单和测试。服务器从数据发布机构直接下载原料，在工作目录完成裁剪和建模。Stage 1 沿用建筑、路网和设施规则，本次城市环境使用标识 `stage1-public-v1`。Stage 2 使用 `2026-11-18-r3`。

## 安装

需要 Python 3.11、POSIX 文件锁和 `osmium` 命令行。Debian/Ubuntu 使用 `apt-get install osmium-tool`，macOS 使用 `brew install osmium-tool`。Python 依赖中的 `osmium` 是读取 PBF 的 Python 库。以下命令从克隆的仓库根目录执行。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
osmium --version
python pipeline.py --help
```

## 从空目录生成 Chicago 的第一份实例

```bash
WORK=/data/evrptwd
CITY=chicago
python pipeline.py --work "$WORK" sources --workers 2
python pipeline.py --work "$WORK" stage1 --city "$CITY" --workers 2
python pipeline.py --work "$WORK" prepare-city --city "$CITY"
python pipeline.py --work "$WORK" plan --city "$CITY"
python pipeline.py --work "$WORK" stage2 --city "$CITY" --split train --n 100 --limit 1
```

第一条 `sources` 默认下载和处理全部 11 城的 Stage 1 来源及公共 Amazon 订单库。多个城市用子进程并行。住宅户数的邻居估计在 11 城共同校准，所以即使本机仅负责 Chicago，也要先准备联合校准输入。第二条命令完成联合校准，再只为 Chicago 构建 CLE、核对产物并签发城市回执。已有该工作目录时直接重跑相同命令。网络中断后保留已完成的下载与城市组件。

默认首次获取 Geofabrik 当前 PBF。需要指定发布日时，把 `--osm-date YYYY-MM-DD` 加到 `sources`。所有机器采用相同参数。公共 AFDC API 默认使用 DEMO_KEY，生产机器可设置环境变量 `AFDC_API_KEY` 使用自己的免费密钥。来源、获取内容和数据条款见[数据说明](docs/SOURCES.zh-CN.md)。

## 多台服务器

可以把[服务器 agent 执行说明](docs/SERVER.zh-CN.md)随仓库地址、提交号和城市分工交给每台机器。

- **不同城市。** 每台从同一 Git 提交运行上述流程，各自设置 `CITY`。第一台得到 `$WORK/public/sources.lock.json` 后，把这份小清单交给其他机器，各机执行 `sources --source-lock /path/to/sources.lock.json --workers 2`。各机自行下载、验 SHA-256 并处理原料。也可以挂载同一组织内部的 `$WORK/raw/` 缓存以减少对上游的重复请求。不同日期的 NSI、AFDC、Census 地址响应或 OSM 文件可能变化。哈希不同则报告具体来源，两次结果归入不同批次。
- **同城多机。** 同城机器使用相同输入锁、软件版本、CPU 架构和 BLAS。各自构建、登记后，对照 `plans/<city>.json` 的 SHA-256。相同清单按下面两条命令各执行一条。节点顺序、路网文件、随机流和计划均绑定；工作路径和本机回执时间可以不同。
- **同机多进程。** `stage1 --city chicago dallas --workers 2` 并行组装两城。Stage 2 的 `--workers 2` 并行两个实例。根据可用内存设置进程数，每个 Stage 2 worker 持有本城路网。
- **正式面板。** 去掉 `--split/--n/--limit` 即执行本机负责的全部槽位。11 城计划汇总后运行 `seal-plans`。收回分片后用 `audit` 检查已完成、未证、失败、遗漏和重复槽位。重跑原命令复用已校验的结果并以原 seed 重试执行异常。首发目标为 17,400 个槽位，四档 CS 预算为 10／30／40／50。[建模和配额](docs/METHOD.zh-CN.md)

```bash
python pipeline.py --work "$WORK" stage2 --city "$CITY" --workers 2 --shard 0/2
python pipeline.py --work "$WORK" stage2 --city "$CITY" --workers 2 --shard 1/2
python pipeline.py --work "$WORK" audit --city "$CITY" --outputs /data/shard0 /data/shard1
```

## 绘图和恢复

```bash
F="$WORK/instances/chicago/train/Cus100/instance0001.evrp.zip"
python pipeline.py --work "$WORK" plot "$F" --out chicago-instance1.png
python pipeline.py --work "$WORK" restore "$F" --out "$WORK/restored/chicago-instance1"
```

每例包含按 depot、客户、CS 排列的 `nodes.txt`，每行给出地点 ID、经纬度、需求、服务时间、时间窗和充电有效功率。`vehicle.json` 保存车辆参数，`orders.json` 等记录逐址订单、选站和见证。恢复命令使用本城 `shared/chicago/` 重建 `costs.npz` 中的四个有向矩阵，并验证哈希。两对矩阵分别给出最短距离路径和最快路径各自的里程与时间。未获见证的实例写入 `uncertified/`，生成失败写入原任务台账。生成阶段记录的路线见证采用不限车数的设置。

## 开发验证

```bash
python -m pytest -q
EVRPTWD_TEST_CATALOG="$WORK/public/amazon" python -m pytest -q
```

第二条会额外执行需要完整 Amazon 订单库的双进程生成、恢复和重试测试。各子命令的完整参数见 `python pipeline.py <命令> --help`。
