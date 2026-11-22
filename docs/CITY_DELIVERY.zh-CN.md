# EVRPTW-D 足额发布与四机交付

四台生成机各自整理负责的城市，汇总机发布 11 城、17,400 个有效实例。每城一份完整 CLE，每例一个 Parquet，CVRP、VRPTW、EVRPTW 共用同一 ID。公开索引按城市、split、N 和实例 ID 排列。公开实例保留日期、depot、车辆、节点及矩阵。实际抽样 seed、任务键和过程回执保存在生产输出。

## 1. 首发配额

| 生成机 | 城市目录名 | 最终有效实例 |
|---|---|---:|
| A | chicago、houston | 3,180 |
| B | new-york、phoenix、jacksonville | 4,680 |
| C | los-angeles、philadelphia、san-antonio | 4,770 |
| D | dallas、fort-worth、san-diego | 4,770 |
| 合计 | 11 城 | 17,400 |

| 规模 | CS | train | val | test1 | test2 | xs | 总数 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Cus100 | 10 | 10,000 | 300 | 500 | 500 | 0 | 11,300 |
| Cus500 | 30 | 2,000 | 300 | 500 | 500 | 0 | 3,300 |
| Cus1000 | 40 | 1,000 | 300 | 500 | 500 | 0 | 2,300 |
| Cus2000 | 50 | 0 | 0 | 0 | 0 | 500 | 500 |
| 合计 | | 13,000 | 900 | 1,500 | 1,500 | 500 | 17,400 |

十个开发城市均分 train、val、test1、xs 的每一格。Jacksonville 独占 test2。按每一格验收，跨城或跨 split 的超额不能填本格缺口。

## 2. 一个缺口怎么补

候选倍率 R 从经验分布中抽取，受所选 depot 的供给上限约束。固定 R=40 用于对照。足迹扩展以 M_target=N×R 为目标，整 tract 会引入取整差异。只要候选足迹有至少 N 个地点，且满足其余构造条件，即可继续构造。正式拒绝以实际台账为准，例如 insufficient_reachable_frontier_capacity 表示最终可达候选数连 N 都没凑够。M 小于 M_target 的合格实例照常保留。

- 已认证且文件完好的实例直接交付。已有认证 ZIP 但缺 Parquet 时从原 ZIP 导出。中断、磁盘错误和文件损坏先恢复同一次执行。
- 对确认由候选容量引起的生成失败，优先沿用原城市、split、N、CS 数、depot 和具体服务日期，按预定 seed 顺序重新调用相同生成器，接纳第一个通过相同认证的实例。客户、订单和站点照常生成。失败与地理条件可能相关，降低 R 或改抽市中心 depot 会改变抽样分布。
- 之前已补好的样本优先核对已有证据。城市、split、N、CS 数、日型、CLE、生成方法及认证一致，且是按原尝试顺序接受的首个合格样本，即可复用。之前若按正常分层规则换过 depot 或日期，核对生成记录并汇报汇总数量。若某 depot 的可供地点总数小于 N，或可用经验 R 支持为空，则保持服务日期不变，按该城既有的分层 depot 顺序选择下一个 depot。只有所有 depot 均已证实存在这类固定容量不足时，才报该槽位在本城无法满足。
- 原始计划、首轮回执、后续尝试和映射保存在生成机，汇总时使用已验收索引核对成员。

## 3. 生成机本轮交付小清单

在单独的发布代码目录检出派单给出的 GitHub commit，沿用生产环境中的 Python 依赖。将 `WORK`、`OUTPUT` 分别设为本机已有工作目录和导出目录的绝对路径。原始计划与来源回执记录实际生成版本，首发生产参考提交为 `e2cf4825dd6a7c4ecd2cf88bef6cb206908ae50e`。回执分别写明生成版本和这次使用的发布脚本版本。

1. 按城市、split、N 盘点现有文件。先核对首轮认证实例和已经补好的实例，包含单独 `supplements/` 目录中的成品。只缺标准位置的 Parquet 时，从已验收的 Parquet 或紧凑 ZIP 做本地导出。
2. 每城运行 `fill`。已有足额城市清单时，核对清单、城市绑定和实例文件摘要后直接复用。未封存的城市先采用已有认证样本；对确有候选容量缺口的槽位，从下一次确定性抽样继续，接受首个认证成功的实例。一条失败和它的成功补样在配额中合计一例。首轮计划、尝试和已存在的补样映射保存在生成机。
3. 按城市、split、N 回报目标数、合格数和仍待处理的异常。已通过逐格配额和去重检查的正式清单可直接验收；中心机按公开索引计数，私有替代表留在生成机。执行错误沿用原任务恢复，其余方法或认证失败保留原因。
4. 回传本机小清单压缩包、SHA-256、生成与发布代码版本、输出根目录、待传实例及 CLE 等文件的总字节数。发回小清单即结束本轮任务。大文件留在生成机，等项目负责人另行通知传输。

```bash
python quota_release.py fill --work "$WORK" --output "$OUTPUT" --city "$CITY"
```

尚未封存且已有独立补样工作目录时，在生成机把既有映射放到 `$WORK/quota-draws/<city>/choices.json`，或给上述命令增加 `--choices /path/to/private-choices.json`。这是生成机内部用的 JSON 数组，每行对象如下。路径填实际成功抽取的原始文件。现有 Parquet 在标准位置时直接使用。若成品另存在 `supplements/`，优先用 `python export_output.py --out "$OUTPUT" instance "$EXISTING_PARQUET"` 在本机导出到标准目录。仅有认证 ZIP 时，把输入改为真实 ZIP 路径。两种方式均沿用现有实例 ID。

```json
[{"split":"train","n":500,"slot":17,"plan":"/actual/draw-plan.json","receipt":"/actual/result.json","archive":"/actual/instance.evrp.zip"}]
```

`slot` 是原缺口。若既有补样确实依照同层抽取选了其他 depot 或日期，复核后在该私有条目加入 `"reviewed_existing_depot_date_change": true`。对采用其他 receipts 格式的既有补样，先核对真实生成参数和认证记录，再适配读取；不可用原失败 seed 或编造计划代替实际来源。

### 打包本机小清单

在 Bash 中设置本机编号 `SERVER`（A、B、C 或 D）和 `OUTPUT` 后执行。逐个列出三个元数据文件，使包内保留汇总机需要的 `_release/<city>/` 相对路径。`files.txt` 自身是一份文件名清单，直接收入包内。压缩包不包含它所列的 CLE 和实例文件。

```bash
(
  set -euo pipefail
  case "$SERVER" in
    A) cities=(chicago houston) ;;
    B) cities=(new-york phoenix jacksonville) ;;
    C) cities=(los-angeles philadelphia san-antonio) ;;
    D) cities=(dallas fort-worth san-diego) ;;
    *) exit 2 ;;
  esac
  files=()
  for city in "${cities[@]}"; do
    for name in index.parquet city.json files.txt; do
      files+=("_release/$city/$name")
      test -s "$OUTPUT/_release/$city/$name"
    done
  done
  archive="$PWD/EVRPTW-D-manifests-$SERVER.tar.gz"
  COPYFILE_DISABLE=1 tar -czf "$archive" -C "$OUTPUT" "${files[@]}"
  sha256sum "$archive"
)
```

每格的实际实例数可从已写出的 `_release/<city>/index.parquet` 按 `split,n` 分组计数。需要整机待传大小时，对各城 `files.txt` 中的相对路径去重，加总 `OUTPUT` 下对应文件的 `stat().st_size`。下面两节分别供缺口处理和后续正式传输时使用。

### 单槽位调试与只读核对

日常生产由 `scripts/generate_cities.sh` 在每城首轮导出后调用 `fill`。自动补额没有固定重试次数。中断后重跑同一命令，已完成的容量拒绝和成功样本按原尝试顺序复用。单独调试一个槽位时可用下面的 `draw --attempts` 指定最后一个尝试号。只核对原有源回执时仍可用 `seal`。

```bash
python quota_release.py draw --work "$WORK" --output "$OUTPUT" \
  --city "$CITY" --split train --n 500 --slot 17 --attempts 8
```

## 4. 已验收城市导出 Hugging Face 分片

每台机器用现有生产输出作输入。把 `OUTPUT` 设为含 `CLE/`、`instances/`、`_release/` 的绝对路径，`SHARD` 设为独立的输出目录。例中 A 负责 Chicago 和 Houston。

```bash
for city in chicago houston; do
  python quota_release.py project --output "$OUTPUT" --out "$SHARD" --city "$city" || exit 1
done
```

此步骤顺序读取已验收文件，把相同的节点和矩阵写入统一的 v2 Parquet。源机核对原文件 SHA-256、求解输入指纹，以及公开文件回读后的节点和四张矩阵摘要。实例 ID、地点顺序、订单属性、车辆、日期、depot 和 CLE 绑定保持一致。原始 seed 与任务键保存在生产输出。重新执行同一命令会核对并复用已经写好的实例。每城完成后，`$SHARD/_release/<city>/` 下产生 `index.parquet`、`city.json`、`files.txt`。其中源索引指纹用于在汇总机比对已验收清单。

为公开实例预留一份额外磁盘空间。每城的 CLE 和来源文件在同一文件系统使用硬链接，跨文件系统会复制。复制完成前保留源文件和分片。共享硬链接的文件按只读数据使用。

## 5. 在 A 汇总发布

四台生成机分别整理公开分片 A、B、C、D。由负责人将 B、C、D 搬到 A，按每城 `files.txt` 中的相对路径保留目录结构。A 的本地分片直接作为输入。将**上次已验收的四份原始小清单**单独解包到 `$ACCEPTED/{A,B,C,D}/_release/<city>/`。源清单与本轮新分片分别使用独立目录。

```bash
# Bash；A、B、C、D 是汇总机上各个公开分片的实际绝对路径
shards=("$A" "$B" "$C" "$D")
accepted=("$ACCEPTED/A" "$ACCEPTED/B" "$ACCEPTED/C" "$ACCEPTED/D")
# READY_DIR 为已有公开版本的只读目录
index="$READY_DIR/index.parquet"
python quota_release.py merge --shards "${shards[@]}" --accepted-shards "${accepted[@]}" --reference-index "$index" --check-only
python quota_release.py merge --shards "${shards[@]}" --accepted-shards "${accepted[@]}" --reference-index "$index" --out "$RELEASE_DIR"
```

预检将每例 ID、求解输入指纹、split、规模、CS 数及生成参数与原清单比对，检查各格配额与全局唯一性。参考索引须与全部公开分片的行、列和排序一致，正式合并逐字节保留该索引和 v2 实例。正式合并核对完整输入资产及输出文件的 SHA-256，完成后原子创建 `RELEASE_DIR`。选择尚不存在的目标目录，与分片同盘可通过硬链接节省空间。仅将以下最终目录作为 Hugging Face dataset 根目录。

```text
EVRPTW-D-release/
  README.md                    # HF 默认配置 instances，catalog 中每行一个实例
  LICENSE.md
  NOTICE.md
  release.json                 # 城市绑定、城市文件大小与哈希、公开索引哈希
  index.parquet                # 原公开总索引
  cities/<city>/shared/        # 原样交付的七份共享文件
  cities/<city>/roads/osm_ways.parquet   # 道路几何、标识、highway 和 name
  instances/<city>/<split>/Cus<N>/<instance_id>.parquet
```

使用者从索引的 `path` 定位实例，按 `city`、`split`、`n` 筛选。索引保留服务日期、depot、p、起始组数 K_seed、实际组数 K_geo、R、M_target、M、tract 数以及内容与文件指纹。K_seed 统计空间组数。每个 Parquet 包含节点、需求、服务时长、时间窗、有效充电功率、车辆和四张矩阵，可独立读出三个求解视图。
