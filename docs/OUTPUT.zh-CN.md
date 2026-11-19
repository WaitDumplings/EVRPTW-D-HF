# 跨求解器交付目录

生成器在 `WORK` 保存原料、阶段产物、任务台账和压缩实例。通过 `export_output.py` 输出到一个独立的数据目录。下例中 Chicago、Dallas 分别保存自己的完整 Stage 1，城市下的每个 Parquet 对应一个 Stage 2 实例。

```text
output/
  CLE/
    chicago/
      cle_manifest.json
      public_authority.json
      stage1_validation.json
      roads/  speeds/  customers/  depots/  charging/  ...
      locations.parquet
      location_access_costs.parquet
      shared/                  # Stage 2 道路、地点、进出接入和边界
    dallas/                    # 发布 Dallas 时创建
  instances/
    chicago/train/Cus100/<instance_id>.parquet
    dallas/val/Cus500/<instance_id>.parquet
```

**矩阵求解器只需一个实例文件。** 要显示真实道路、核对地点来源或重新路由时，再取 `CLE/<city>/`。城市目录保存源 CLE 清单列举的全部文件、城市验收回执，以及该版本对应的 `shared/`。这些已交付文件都可以复制到另一台机器。个别 Stage 1 来源记录中保留了构建时的路径，求解和成本重建使用交付目录的相对位置。

## 导出一个正式实例

从仓库根目录执行，下列 `OUTPUT` 可以是另一个磁盘。先完成 Stage 1 和 `prepare-city`。同一输出根中每城使用一个确定版本；同城多机共用同一份已验收的发布 CLE。

```bash
OUTPUT=/data/evrptwd-output
python export_output.py --out "$OUTPUT" cle --work "$WORK" --city chicago
F="$WORK/instances/chicago/train/Cus100/instance0001.evrp.zip"
FILE=$(python export_output.py --out "$OUTPUT" instance "$F")
python export_output.py --out "$OUTPUT" plot "$FILE" --dest chicago-cus100.png
```

如果已经用 `pipeline.py restore` 恢复过实例，可以将最后的输入 `"$F"` 换成已恢复的目录，直接复用四个矩阵。导出器核对城市、联合校准、CLE 清单和运行时道路版本。相同实例重复导出时按逻辑内容复用已有文件，同路径出现不同内容或另一版城市则报错。输出在完成后原子可见。发布器使用支持 POSIX 文件锁和同一文件系统硬链接的输出盘，交给求解器的文件可以常规复制。

## 单文件契约

一行代表一个节点。按 `node_idx` 升序读取：索引 0 为 depot，1 至 N 为客户，其余为 CS。四个成本列均为 `list<float64>`，每行列表的第 j 项是本行起点到节点 j 的成本。矩阵有向、有限、非负、零对角；所有列表长度均等于节点数。源采样记录、订单 donor 和路线见证随原始 ZIP 归档。

| 列 | 单位与含义 |
|---|---|
| shortest_distance_m | 最短距离路径的米数，主目标使用这一列 |
| shortest_distance_time_s | 同一条最短距离路径的秒数，默认执行时间 |
| fastest_time_distance_m | 最快路径的米数 |
| fastest_time_s | 同一条最快路径的秒数 |

Parquet 文件键值元数据 `evrptwd.instance` 是 UTF-8 JSON。`schema=evrptwd_solver_instance_v1`。其中 `payload.instance` 给出 `instance_id`、车型 JSON、日型、节点数和矩阵哈希；`payload.cle` 给出城市、源 CLE 清单、共享资产及校准的 SHA-256；`payload.rules` 给出单位和求解语义。内容摘要覆盖这些信息、逻辑节点摘要和矩阵摘要。矩阵按行主序、小端 float64 求哈希，允许更换 Parquet 压缩编码或物理行顺序。

CVRP 使用仓库和客户的距离子矩阵。VRPTW 同时使用同路径行驶时间、服务时间、硬服务开始窗和班次。EVRPTW 加入 CS，耗电量为里程 km 乘车型系数 kWh/km，车辆到达节点含回仓时满足最低 SOC。站点按有效功率 kW 线性充满，可以复访，depot 负责出发和回仓。时间以当地午夜起的秒数计，可等待。各求解器报告的总米数使用原始 float64 距离，包含各路线首尾与仓库之间的有向路段。只支持整数的求解器记录内部缩放与取整方式，最终在原始矩阵上重算总米数。

## 交给求解器

将 CLE 和实例复制到另一台机器后，可在接收端运行 `python export_output.py --out "$OUTPUT" verify-cle --city chicago --instance "$FILE"`，核对城市文件的完整性和实例引用的城市版本。

```python
from instance_file import read_instance

task = read_instance("my-chicago-instance.parquet", "vrptw")
D = task.matrices["shortest_distance_m"]
T = task.matrices["shortest_distance_time_s"]
# route 是 [0, 客户索引, ..., 0]。总距离以原始 float64 路段累加。
distance_m = sum(D[i, j] for i, j in zip(route, route[1:]))
```

Python 求解环境只需这两个读取模块 `instance_file.py`、`solver_parquet.py`，以及 NumPy 和 PyArrow。可以将两个模块随求解器放置。

对于没有 Arrow 的求解器，可先在有 Python 的机器上转换一次，然后只复制转换后的目录：

```bash
python export_output.py unpack "$FILE" --problem evrptw --dest "$OUT/for-cpp"
```

读取 `nodes.tsv`、`vehicle.json` 和 `instance.json`。JSON 中保留 CLE 版本绑定、源实例逻辑摘要以及前两个文件的字节 SHA-256，接收方可用标准 SHA-256 工具复核。后者的 `matrices` 列出每个 `.f64le` 文件的名称、形状和摘要。对 n 列矩阵，单元 (i,j) 的字节偏移为 `8*(i*n+j)`，直接按 IEEE 754 小端双精度读取。节点、工况、车辆和可用矩阵随 `--problem cvrp|vrptw|evrptw` 自动裁切；每种视图保留原 `instance_id`。

收集发布文件时使用 `instances/*/*/Cus*/*.parquet`，城市目录按已发布城市名枚举。中断写入的实例暂存文件使用隐藏的 `.partial` 后缀，城市暂存目录以点开头。正式上传 Hugging Face 时，可将这些单实例文件作为 `export_instance.py --input-list` 的输入，打包成可浏览的实例索引、节点、矩阵行三张分片表。一个实例的逻辑数据在两种布局间保持相同。[HF Parquet 布局](PARQUET.zh-CN.md)
