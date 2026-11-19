# 四台服务器分别生产，按城市合并发布

各机从空目录开始，在本机从公开源准备负责城市的输入、按城拟合住宅户数、构建 CLE、生成并导出实例。同一台机器上的不同城市也使用各自的住宅参照集、拟合阈值和充电功率补值。公共 Amazon 订单库和全国或州级的公开下载件可在本机缓存。

| 服务器 | 城市 | 预登记槽位 |
|---|---|---:|
| A | Chicago、Houston | 3,180 |
| B | New York、Phoenix、Jacksonville | 4,680 |
| C | Los Angeles、Philadelphia、San Antonio | 4,770 |
| D | Dallas、Fort Worth、San Diego | 4,770 |
| 合计 | 11 城 | 17,400 |

十个开发城每城 1,590 例。Cus100、500、1000 的训练配额分别为 1,000、200、100，每档验证 30、Test1 50。Cus2000 每城只做 50 个 XS。Jacksonville 只做 Test2，前三档各 500。四档分别配 10、30、40、50 座外部站。[完整配额和设计依据](METHOD.zh-CN.md)

## 各机启动

同一版 Git 提交、Python 3.11、系统 osmium-tool 和仓库 requirements.txt。首次执行时使用新的工作目录与交付目录。命令在 Bash 中执行，将最后一个参数换成本机字母。

```bash
export WORK=/data/Maojie/EVRPTW_DB/city-v2-work
export OUTPUT=/data/Maojie/EVRPTW_DB/city-v2-output
export WORKERS=2 CLE_WORKERS=1 SOURCE_WORKERS=2
bash scripts/generate_cities.sh A
```

各机可同时启动。默认按城串行构建 CLE，`CLE_WORKERS` 可并行构建本机多城。`WORKERS` 控制同城实例进程数，每进程加载一份路网和订单库。中断后使用同一命令与目录续跑，保留原任务、seed 和失败原因。Chicago 的首槽也从本城新 CLE 生成。

每台交付整个 `city-v2-output/`。其中 `CLE/<city>/` 为本城环境，`instances/<city>/<split>/Cus<N>/` 为认证实例，`reports/<city>/` 含计划、逐槽状态、实例 SHA-256、审计及导出计数。查看审计里的 `issues` 和 `pending`，修复执行错误后原地续跑。构造失败和未获见证的槽位按原状态交付。原始下载、ZIP 和详细尝试台账留在生成机的工作目录。

## 合并成数据集

把四份输出放到汇总机可读的四个目录，再执行以下命令。目标目录由命令创建。同盘优先用硬链接，跨盘复制。汇总机只消费成品。

```bash
python merge_dataset.py --shards /incoming/A /incoming/B /incoming/C /incoming/D \
    --out /data/EVRPTW-D
```

汇总时逐城核对配额、终态、CLE 绑定、实例 ID、CS 数和文件摘要。随包 README 将 Hugging Face Viewer 的默认表设为实例索引，另提供含全部构造结果的 slots 视图。每份认证实例保留一个 Parquet，供 CVRP、VRPTW 和 EVRPTW 三种视图使用。根目录的 `index.parquet` 每行对应一个可读实例，含城市、split、规模、站数、ID 和相对路径。`slots.parquet` 每行对应一个计划槽位，包括未发布原因。`release.json` 汇总实际发布数与各城来源、拟合及 CLE 指纹。`reports/<city>/plan.json` 保存日期、仓库和运行环境。
