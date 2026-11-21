# EVRPTW-D

从公开原始数据构造美国 11 城的电动货车配送实例。仓库提供 Python 代码、拟合后的模型参数、来源清单和测试。服务器从数据发布机构直接下载原料，在工作目录完成裁剪和建模。Stage 1 对每座城市独立拟合住宅户数，并在本城内补全缺失的兼容充电功率。城市回执标识为 `evrptwd_local_city_v1`。Stage 2 使用 `2026-11-18-r3`。

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
python pipeline.py --work "$WORK" sources --city "$CITY" --workers 2
python pipeline.py --work "$WORK" stage1 --city "$CITY" --workers 2
python pipeline.py --work "$WORK" prepare-city --city "$CITY"
python pipeline.py --work "$WORK" plan --city "$CITY"
python pipeline.py --work "$WORK" stage2 --city "$CITY" --split train --n 100 --limit 1
```

以上命令只准备 Chicago 的地理原料和公共订单库。Stage 1 的近邻、质量阈值和同类端口功率均从该城计算。已有同版工作目录时，重跑原命令即可续跑。

默认采用 Geofabrik 的 2026-11-01 有日期的 PBF。需要指定发布日时，把 `--osm-date YYYY-MM-DD` 加到 `sources`。来源锁会记录该选项，其他机器传入锁后自动沿用。公共 AFDC API 默认使用 DEMO_KEY，生产机器可设置环境变量 `AFDC_API_KEY` 使用自己的免费密钥。来源、获取内容和数据条款见[数据说明](docs/SOURCES.zh-CN.md)。

## 多台服务器

四台机器各自按城市从公开源构造 CLE 和实例，共 17,400 个预登记槽位。各机的生成命令见[批量生产说明](docs/BATCH.zh-CN.md)。批量入口在每城导出后执行 `quota_release.py fill`，复用已有补样并自动重抽仍有候选容量缺口的槽位，直至该城各组足额。四台先回传每城的三份小清单，汇总机用 `merge --check-only` 预检 17,400 例。[逐城配额、清单打包和正式汇总](docs/CITY_DELIVERY.zh-CN.md)

## 绘图和恢复

```bash
F="$WORK/instances/chicago/train/Cus100/instance0001.evrp.zip"
python pipeline.py --work "$WORK" plot "$F" --out chicago-instance1.png
python pipeline.py --work "$WORK" restore "$F" --out "$WORK/restored/chicago-instance1"
```

每例包含按 depot、客户、CS 排列的 `nodes.txt`，每行给出地点 ID、经纬度、需求、服务时间、时间窗和充电有效功率。`vehicle.json` 保存车辆参数，`orders.json` 等记录逐址订单、选站和见证。恢复命令使用本城 `shared/chicago/` 重建 `costs.npz` 中的四个有向矩阵，并验证哈希。两对矩阵分别给出最短距离路径和最快路径各自的里程与时间。未获见证的实例写入 `uncertified/`，生成失败写入原任务台账。生成阶段记录的路线见证采用不限车数的设置。

## 交给其他求解器

用 `export_output.py` 将已验收城市放在 `output/CLE/<city>/`，将每个实例放在 `output/instances/<city>/<split>/Cus<N>/<instance_id>.parquet`。矩阵求解器可以只复制一个 Parquet；需要底层道路的求解器再复制该城市的 CLE。独立文件同时支持 CVRP、VRPTW 和 EVRPTW，以及不依赖 Arrow 的 C++ 二进制读取。[目录、格式与命令](docs/OUTPUT.zh-CN.md)

## 导出 CVRP、VRPTW 和 EVRPTW

从一份已恢复的任务生成可浏览的 Parquet 数据集。三种问题共用一个 `instance_id`，客户顺序和需求相同。默认写完整 EVRPTW，CVRP、VRPTW 可在读取时选取，也可独立导出。

```bash
python export_instance.py "$WORK/restored/chicago-instance1" --out "$WORK/parquet/chicago-instance1"
```

求解端命令、三表结构、矩阵行列顺序、按 split 和城市分片的布局见[Parquet 格式](docs/PARQUET.zh-CN.md)。

## 开发验证

运行 `python -m pytest -q`。本机已有完整 Amazon 订单库时，使用 `EVRPTWD_TEST_CATALOG="$WORK/public/amazon" python -m pytest -q`，同一轮会覆盖双进程生成、恢复和重试。
