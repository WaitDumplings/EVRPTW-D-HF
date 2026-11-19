# 一份配送任务的三种求解格式

`instance_id` 标识已经实际生成的一份配送任务。三种格式沿用同一天、depot、客户次序、逐户需求与 split。以总道路距离为目标；车数无硬上限，每车一趟，同仓出发并回仓，每个客户整单服务一次。客户来自当前生成器的住宅库存和单程能量准入域。

| problem | 求解节点 | 节点和车辆设置 | 有向道路矩阵 |
|---|---|---|---|
| cvrp | depot、客户 | 需求和载货容量 | 最短距离 |
| vrptw | depot、客户 | 同上，加服务时长、服务开始窗、工作班次 | 最短距离、该路径的行驶时间 |
| evrptw | depot、客户、入选 CS | 同上，加有效充电功率、电池和充电规则 | 上述两张，加最快路径上的距离、最快行驶时间 |

## 单个实例的导出与读取

在仓库根目录，用上一阶段已经恢复的目录作为输入。也可直接传入一个正式生成的 `.evrp.zip` 并提供 `--work "$WORK"`，入口会调用原恢复器核对并展开压缩包。

```bash
python export_instance.py "$OUT/restored" --out "$OUT/hf"
ID=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["instance_id"])' "$OUT/restored/instance.json")
python export_instance.py "$OUT/hf" --instance-id "$ID" --problem cvrp  --out "$OUT/cvrp"
python export_instance.py "$OUT/hf" --instance-id "$ID" --problem vrptw --out "$OUT/vrptw"
```

每个输出目录可独立读取。只在求解时选择问题类型，可以直接使用同一份 `hf/`。读入时核对节点和所用矩阵的 SHA-256。

```python
from solver_parquet import load_problem

# hf_dir 是本地数据目录；instance_id 来自 instances 表。
task = load_problem(hf_dir, instance_id, "vrptw")
demand = task.nodes["demand_cm3"].to_numpy()
dist_m = task.matrices["shortest_distance_m"]
time_s = task.matrices["shortest_distance_time_s"]
```

## Hugging Face 发布布局

发布一次完整 EVRPTW Parquet 即可供三种求解器使用。发布目录包含三个 Dataset Viewer 配置，首页默认显示实例索引。目录内按原始 split、城市、客户数分区，每张表按约 256 MiB 的未压缩 Arrow 数据独立分片，采用 Zstandard 压缩。上传时保留自动生成的 README 和相对路径，用户可以分别浏览或下载各表。[Hub 配置说明](https://huggingface.co/docs/hub/datasets-manual-configuration)

```text
README.md
instances/train/chicago/Cus100/part-00000.parquet
nodes/train/chicago/Cus100/part-00000.parquet
matrix_rows/train/chicago/Cus100/part-00000.parquet
```

| 表 | 一行代表 | 主要字段与读取方式 |
|---|---|---|
| instances | 一份配送任务 | `instance_id`、城市、split、日期、seed、depot、客户数、站数、车辆容量、方法版本、问题类型。车辆规则保存在 `vehicle_json`，节点及矩阵摘要用于校验。 |
| nodes | 一个求解节点 | `(instance_id, node_idx)` 唯一。保存稳定的 `location_id`、节点类型、经纬度和需求量（cm³）。时间字段以当地午夜起算的秒计，功率以 kW 计。 |
| matrix_rows | 一个起点到所有终点 | `(instance_id, from_idx)` 唯一。每项成本是一列 `list<float64>`，列表第 j 项对应 `node_idx=j`。保留小数精度、去向与回向。 |

例如 Chicago Cus100 的共享版有 1 行索引、111 行节点、111 行矩阵。客户排在 depot 后，10 个 CS 置于最后。CVRP 和 VRPTW 都使用前 101 个节点，且同时截取对应矩阵的前 101 行、前 101 列。VRPTW 采用最短距离路径自身的行驶时间。完整 EV 版的四个矩阵键与 `costs.npz` 相同。

浏览实例和住宅分布只需打开前两张表。`matrix_rows` 供需要精确路网成本的求解器单独下载。长期归档可继续使用原始轻量 ZIP 和按城市共享的道路资产；其中保留订单来源、选站记录和可行见证。Parquet 中的 `source_protocol_status` 指原 EV 实例的见证状态。没有分配正式 split 的本地例子保留空值，在 Viewer 中归入 `example`。

多例发布时，导出器接受多个已完成实例，或通过 `--input-list` 读取每行一个输入路径的文件。每个输入只展开一次，重复的 `instance_id` 会报错。只在所有输入完成后写出目标目录。示例命令为 `python export_instance.py --work "$WORK" --input-list completed.txt --out hf-release`。


## Chicago Cus100 本地对照

使用六个 tract 的样例 `chicago-r3-cus100-ring0`。从已经恢复的 EV 实例导出完整 Parquet，再从这一份 Parquet 导出 CVRP 和 VRPTW，独立打开三套文件比对原始 ZIP 中的节点以及已恢复的路网矩阵。三套的客户 ID、次序和逐户需求全部一致，总需求为 2,796,562.364 cm³。VRPTW 和 EVRPTW 的服务时长和窗口完全一致，EVRPTW 的 10 个站点及功率与原实例一致。

| 格式 | 节点 | 校验的 float64 矩阵值数 | 与源矩阵对应切片的字节 |
|---|---:|---:|---|
| CVRP | 101 | 10,201 | 全部一致 |
| VRPTW | 101 | 20,402 | 全部一致 |
| EVRPTW | 111 | 49,284 | 全部一致 |
