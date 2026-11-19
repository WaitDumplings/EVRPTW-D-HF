# 17,400 个槽位的批量生产交接

使用已通过 Chicago 单例验收的 11 城联合住宅校准和 r3 生成器。三台机器按城市分工，每城在一台机器上多进程生成。工作范围到实例交付与台账汇总。逐城、逐规模配额和分配依据见[方法第 2 节](METHOD.zh-CN.md)。

| 服务器 | Stage 2 城市 | 槽位 | 本轮新增待处理 |
|---|---|---:|---:|
| A（Chicago 验收机） | chicago、houston、fort-worth | 4,770 | 4,769 |
| B | new-york、phoenix、dallas、jacksonville | 6,270 | 6,270 |
| C | los-angeles、philadelphia、san-antonio、san-diego | 6,360 | 6,360 |
| 总计 | 11 城 | **17,400** | **17,399** |

### 各机检出

在 Bash 中使用交接消息给出的完整提交号作为 `RELEASE_COMMIT`。新机器运行 `git clone https://github.com/WaitDumplings/EVRPTW-D-HF.git` 后进入仓库。已有 checkout 的机器直接进入原仓库。

```bash
git fetch origin
git checkout --detach "${RELEASE_COMMIT:?使用交接消息中的完整提交号}"
```

A 沿用验收时的 Python 环境。B、C 采用与 A 相同的运行环境；Python、依赖、CPU 架构和 BLAS 版本可从 A 的 `plans/chicago.json` 中的 `runtime` 读取。以下命令从仓库根目录执行。每个 Stage 2 进程载入自己的路网和订单库，按本机可用内存设 `WORKERS`。

## Server A

沿用原路径。联合校准摘要为 `0945a2cf0655259880b90c425d7877e0cb3c969215fe322ade8d33390f200911`。已有的 Chicago CLE、shared、城市计划、首槽台账、ZIP 和 Parquet 都留在原位。下面两枚文件指纹来自单例验收包。若旧文件缺失，先从原验收工作目录或其备份找回。

```bash
set -euo pipefail
WORK=/data/Maojie/EVRPTW_DB/evrptwd-work
OUTPUT=/data/Maojie/EVRPTW_DB/evrptwd-output
FIRST=7e21cd8cd93f6dfcd657ca02
test -s "$WORK/plans/chicago.json"
test -s "$WORK/ledger/chicago/train/Cus100/instance0001/result.json"
printf '%s  %s\n' \
  dfa75ce1ceefdd19d7dd4650f85d4a9c588c8a92078aaac74a5302011099d8d5 "$WORK/instances/chicago/train/Cus100/instance0001.evrp.zip" \
  93b3344800a6c6ed37cdf736e18cff3b693efbb52f94a31ef480e77ce63f5f40 "$OUTPUT/instances/chicago/train/Cus100/$FIRST.parquet" | sha256sum -c -

OTHERS=(new-york los-angeles houston phoenix philadelphia san-antonio san-diego dallas fort-worth jacksonville)
python pipeline.py --work "$WORK" stage1 --phase cities --city "${OTHERS[@]}" --workers "${CLE_WORKERS:-2}"
python pipeline.py --work "$WORK" stage1 --phase seal
for city in "${OTHERS[@]}"; do
    python pipeline.py --work "$WORK" prepare-city --city "$city"
    python pipeline.py --work "$WORK" plan --city "$city"
    python export_output.py --out "$OUTPUT" cle --work "$WORK" --city "$city"
done
python pipeline.py --work "$WORK" seal-plans
```

`--phase cities` 读取现有校准文件和各城住宅表，只补十城 CLE。`--phase seal` 汇总 11 城回执。`seal-plans` 生成共同的 `plans/release.json`，应显示 17,400 个槽位。把下表中的输入通过共享盘或现有内部传输渠道交给 B、C，随附发布索引的 SHA-256 和其中的校准摘要。然后设置 `CITIES=(chicago houston fort-worth)`，执行下方“逐城生成和导出”。

| 交接项 | 在接收机上的位置 |
|---|---|
| 同一份发布索引和本机负责城市的计划 | `$WORK/plans/release.json`、`$WORK/plans/<city>.json` |
| A 已处理的 Amazon 订单库，含 receipt 及数据文件 | `$WORK/public/amazon/` |
| 本机负责城市发布的完整 CLE，含 shared 子目录 | `$OUTPUT/CLE/<city>/` |

A 留存公开来源锁 `public/sources.lock.json`、联合校准和 11 城回执。B、C 可挂载只读输入，并使用本机可写的 `$WORK`、`$OUTPUT/instances/` 存放新产物。

## Server B

接收 A 的发布索引、订单库和本机四城 CLE、城市计划。设置 `WORK` 和 `OUTPUT` 为本机对应的绝对目录，设置 `CITIES=(new-york phoenix dallas jacksonville)`。Jacksonville 只生成 `test2`。执行“工作机接入”和“逐城生成和导出”，共 6,270 个槽位。

## Server C

接收 A 的发布索引、订单库和本机四城 CLE、城市计划。设置 `WORK` 和 `OUTPUT` 为本机对应的绝对目录，设置 `CITIES=(los-angeles philadelphia san-antonio san-diego)`。执行“工作机接入”和“逐城生成和导出”，共 6,360 个槽位。

Jacksonville 的发布路径为 `instances/jacksonville/test2/Cus<N>/`。已有旧版城市计划时保留原文件和台账，切到本次提交后直接续跑。任务键沿用原登记值，以便同一任务保留 seed 和实例 ID。旧计划和 ZIP 中的 `test3` 在导出和审计时归入 `test2`。如果已按旧名导出过部分文件，交付时仅收录 `test2/` 下的 Parquet。

### 工作机接入（B、C）

核对 A 提供的发布索引 SHA-256。只需把 Stage 2 使用的城市路径指向已交付的 shared 目录。若使用共享磁盘且这些路径已经就位，可直接进入下一步。

```bash
set -euo pipefail
: "${WORK:?设置工作目录}" "${OUTPUT:?设置发布目录}"
mkdir -p "$WORK/shared"
sha256sum "$WORK/plans/release.json"
for city in "${CITIES[@]}"; do
    if [[ ! -e "$WORK/shared/$city" ]]; then
        ln -s "$OUTPUT/CLE/$city/shared" "$WORK/shared/$city"
    fi
done
```

### 逐城生成和导出（A、B、C）

```bash
set -euo pipefail
mkdir -p "$WORK/reports"
for city in "${CITIES[@]}"; do
    python pipeline.py --work "$WORK" stage2 --city "$city" --workers "${WORKERS:-2}"
    python pipeline.py --work "$WORK" audit --city "$city" > "$WORK/reports/$city.audit.json"
    python export_output.py --out "$OUTPUT" city-instances --work "$WORK" --city "$city" \
        > "$WORK/reports/$city.publish.json"
done
```

中断时沿用同一计划、目录和 seed，修复可恢复的执行异常后再跑原命令。若个别城市仍有执行故障，先保存该城审计，再继续本机其他城市。生成器会跳过已登记且 ZIP 摘要一致的槽位；批量导出只处理已认证槽位。已有 Parquet 会直接与认证 ZIP 的客户、车辆、充电站、四张矩阵摘要及城市绑定逐项核对，匹配后计入 `reused`。每个实例交付一个 Parquet，三种求解视图共用。

每台交回逐城的 audit、publish 两份 JSON、同一发布索引的 SHA-256，以及 Parquet、ZIP、ledger 和未认证产物的位置。按城市、N、split 汇总计划、已认证、未获见证、证实不可行、构造失败、执行异常和待处理数；分别检查 audit 的 `issues` 与各行 `pending`。该分组已发布数为 `published + reused`，应等于认证数。失败和未获见证的槽位保留原记录和原因。

A 按城市归并 B、C 的实例目录和台账（共享盘只汇总一次），提交全局计划数、认证数、实际发布数及差额原因。Chicago 首槽计入 A 的 Cus100/train 一次。完成数据交付即结束本轮。
