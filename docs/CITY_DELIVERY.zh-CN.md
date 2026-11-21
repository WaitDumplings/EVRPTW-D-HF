# EVRPTW-D 足额发布与四机交付

四台生成机各自整理负责的城市，汇总机发布 11 城、17,400 个有效实例。每城一份完整 CLE，每例一个 Parquet，CVRP、VRPTW、EVRPTW 共用同一 ID。公开索引按城市、split、N 和实例 ID 排列。每例均保留实际生效的 seed、日期、depot、车辆和节点属性。数据集说明按组汇报构造拒绝情况。

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
- 之前已补好的样本优先核对已有证据。城市、split、N、CS 数、日型、CLE、生成方法及认证一致，且是按原尝试顺序接受的首个合格样本，即可复用。之前若按正常分层规则换过 depot 或日期，核对生成记录并汇报汇总数量。若某 depot 可供地点总数本来就小于 N，或可用经验 R 支持为空，换 seed 不能解决；列出该分组交项目负责人决定如何处理。
- 记录保存在生成机。原始计划、首轮失败回执、后续尝试和新旧映射用于内部追溯。数据集卡写明“在相同构造条件下抽样至各组足额”，可按城市、规模、split 汇报拒绝数量。首轮拒绝率的分母是原计划槽位数；若另算全程拒绝率，分母是去重的实际随机抽取次数，排除执行错误和同一抽取的文件重导出。

## 3. 生成机本轮交付小清单

在单独的发布代码目录检出派单给出的 GitHub commit，沿用生产环境中的 Python 依赖。将 `WORK`、`OUTPUT` 分别设为本机已有工作目录和导出目录的绝对路径。原始计划与来源回执记录实际生成版本，首发生产参考提交为 `e2cf4825dd6a7c4ecd2cf88bef6cb206908ae50e`。回执分别写明生成版本和这次使用的发布脚本版本。

1. 按城市、split、N 盘点现有文件。先核对首轮认证实例和已经补好的实例，包含单独 `supplements/` 目录中的成品。只缺标准位置的 Parquet 时，从已验收的 Parquet 或紧凑 ZIP 做本地导出。
2. 每城运行 `seal`。它读取首轮的 `reports/<city>/plan.json`、`slots.parquet`、实际 ZIP、回执与实例文件。已有补样的私有映射用下文的 `--choices`。首轮计划、失败记录和所有真实 seed 保留在生成机。
3. 按组回报目标数、已核对的正式实例数、缺口，以及原因为容量拒绝或执行异常的槽位。缺口或证据不一致时，交回已经核对的城市并写明哪些城市待处理。此次只复用和核对已有实例，新抽样单独安排。
4. 回传本机小清单压缩包、SHA-256、生成与发布代码版本、输出根目录、待传实例及 CLE 等文件的总字节数。发回小清单即结束本轮任务。大文件留在生成机，等项目负责人另行通知传输。

```bash
python quota_release.py seal --work "$WORK" --output "$OUTPUT" --city "$CITY"
```

已有独立补样工作目录时，给上述命令增加 `--choices /path/to/private-choices.json`。这是生成机内部用的 JSON 数组，每行对象如下。路径填实际成功抽取的原始文件。现有 Parquet 在标准位置时直接使用。若成品另存在 `supplements/`，优先用 `python export_output.py --out "$OUTPUT" instance "$EXISTING_PARQUET"` 在本机导出到标准目录。仅有认证 ZIP 时，把输入改为真实 ZIP 路径。两种方式均沿用现有实例 ID。

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

### 后续经确认的缺口处理

确实尚未补齐、而且原失败是可通过重抽解决的容量缺口时，可使用内置 draw 命令。以下是假设的槽位，仅说明用法。`--attempts` 表示本次允许运行至第几个尝试号；续跑保持同一顺序，取首个合格样本。工具在原 WORK 的 quota-draws 下保存少量独立尝试；seal 自动找到其 choice.json。

```bash
python quota_release.py draw --work "$WORK" --output "$OUTPUT" \
  --city "$CITY" --split train --n 500 --slot 17 --attempts 8
```

## 4. 汇总机先收清单，后收文件

约 300 GB 是预估。各机 _release/<city>/ 中只有 index.parquet、city.json 和 files.txt。先把这些小文件放到汇总机 incoming/A/_release/<city>/ 等位置，汇总机预检通过后，待项目负责人另行通知传输。各机 files.txt 的路径均相对于本机 OUTPUT。汇总机以下命令在同版本生成器仓库内执行。

```bash
python quota_release.py merge --check-only --shards incoming/A incoming/B incoming/C incoming/D
```

预检核对 11 城归属、逐格配额、CS 数、配置版本、实例 ID 和去掉 ID 后的求解输入摘要。输出中的 instance bytes 只计实例。汇总机另为 CLE、分片元数据和文件系统本身留空间。在汇总机从来源拉取时，以下示例将 SERVER_A、远端绝对路径和城市替换为实际值。

```bash
rsync -aP --files-from=incoming/A/_release/chicago/files.txt \
  SERVER_A:/absolute/path/city-v2-output/ incoming/A/
```

四台都完成后执行正式合并。incoming 和最终目标放在同一个支持硬链接的文件系统，选择尚不存在的输出目录。工具将认证实例和 CLE 原文件硬链接进发布目录，逐文件核对源清单 SHA-256。完成后原子发布。硬链接共享底层文件；汇总后也不要原地改写 incoming 里的文件。

```bash
python quota_release.py merge --shards incoming/A incoming/B incoming/C incoming/D \
  --out /same-filesystem/EVRPTW-D-release
```

## 5. 最终交给 Hugging Face 和求解器的内容

```text
EVRPTW-D-release/
  README.md
  NOTICE.md、docs/SOURCES.zh-CN.md                  # 数据来源与署名
  release.json                                      # 版本、各城 CLE 指纹、首轮容量拒绝聚合计数
  index.parquet                                     # 17,400 行，每个正式实例一行
  config/                                           # 实际生成配置，同一版本一份
    vehicle.json                                    # 车型、容量、能耗、SOC、充电与班次
    customer_profile.json                           # 客户概率、组数、道路准入、经验 R 来源
    candidate_ratio.csv                             # R 的经验分布
    order_profile.json                              # 订单与服务设置
    …                                               # 生产版本的其余配置
  parameters/cities/<city>/                         # 本城数据来源与住宅参数校准
  CLE/<city>/                                       # 完整 Stage 1，含道路、地点及 shared/
  instances/<city>/<split>/Cus<N>/<instance_id>.parquet
```

公开索引含实际日期、depot、seed、p、K_seed、K_geo、抽到的 R、M_target、实际 M、tract 数、站数、相对路径和文件摘要。每个实例本身带有节点（含需求、服务时间、时间窗、有效充电功率）、车辆 JSON 和四张有向路网矩阵。每个公开实例使用相同字段和目录。公开索引没有首轮/补样列。原 Parquet 保留真实生成信息；有不同 seed 不等于应改写它。

merge 成功后只发布 EVRPTW-D-release/。生成机的逐槽台账以及传输用的 incoming/ 各自保留。若迁移机器有独立归档需求，可单独备份紧凑 ZIP 和台账。
