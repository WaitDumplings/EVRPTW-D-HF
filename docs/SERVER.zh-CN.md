# 给 server agent 的单例验收 prompt

将仓库 URL、这次交付的完整 Git 提交号和可写数据盘路径发给 agent，并附以下任务。

请检出指定提交，使用 Python 3.11、requirements.txt 和系统命令 osmium-tool。本轮只验收 1 个 Chicago / train / Cus100。先下载公开源，完成 11 城联合住宅户数校准，再构建 Chicago 的城市环境，使用首个登记槽位。已有缓存和同版本产物时可续用。各命令均从仓库根目录运行。

```bash
set -euo pipefail
WORK=/data/evrptwd-work
OUTPUT=/data/evrptwd-output
python pipeline.py --work "$WORK" sources --workers 2
python pipeline.py --work "$WORK" stage1 --city chicago --workers 2
python pipeline.py --work "$WORK" prepare-city --city chicago
python pipeline.py --work "$WORK" plan --city chicago
python pipeline.py --work "$WORK" stage2 --city chicago --split train --n 100 --limit 1 --workers 1

F="$WORK/instances/chicago/train/Cus100/instance0001.evrp.zip"
OUT=$(mktemp -d "$WORK/chicago-cus100-check.XXXXXX")
python pipeline.py --work "$WORK" restore "$F" --out "$OUT/restored"
python export_output.py --out "$OUTPUT" cle --work "$WORK" --city chicago
FILE=$(python export_output.py --out "$OUTPUT" instance "$OUT/restored")
python export_output.py --out "$OUTPUT" plot "$FILE" --dest "$OUT/chicago-cus100.png"
python export_output.py verify-instance "$FILE" --restored "$OUT/restored" | tee "$OUT/solver-check.json"
```

首个槽位若失败或未获路线见证，从 `$WORK/ledger/chicago/train/Cus100/instance0001/result.json` 报告原因，保留原 seed。

从同一个 Parquet 核对 1 个 depot、100 个客户、10 座 CS，以及 CVRP／VRPTW 的 101×101 子矩阵和 EVRPTW 的四张 111×111 有向矩阵。把 Chicago 的 PNG 作为附件发给我，同时给出提交号、CLE 路径、实例 Parquet 路径、实例 ID 和验收结果。到此停止，等待我的批量运行指令。
