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

每台生成机保留已有的 `city-v2-work/` 和 `city-v2-output/`。前者保存认证 ZIP 与尝试台账，后者含本机城市的 CLE、求解器实例和逐槽回执。

## 生产完成后的交付

足额公开版使用 `quota_release.py`。各机按首轮回执和已认证的补样逐城核对现有文件，先回传小清单。汇总机预检后，由项目负责人另行安排大文件传输。[配额、补样复用、清单打包与汇总](CITY_DELIVERY.zh-CN.md)

`merge_dataset.py` 用于按首轮计划汇总所有槽位及其状态，保留该版本的审计用途。
