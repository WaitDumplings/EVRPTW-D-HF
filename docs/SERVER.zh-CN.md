# 给 server agent 的执行说明

从指定 GitHub 仓库克隆指定提交，按 README 安装 Python 3.11 和 osmium-tool，检查工作盘空间。仅从仓库登记的公共数据发布方获取原始数据。若有本批来源锁，在 sources 命令中传入 --source-lock；按分工设置城市、OSM 发布日和进程数。逐步执行 sources、stage1、prepare-city、plan 和 stage2。先用 --split train --n 100 --limit 1 生成一个样例，恢复矩阵并绘图，确认后按分片生成正式任务。Jacksonville 的样例使用 --split test3。记录当前 Git 提交、public/sources.lock.json、城市计划哈希、stage1 日志和各任务台账。多机结果按原槽位 audit。有来源哈希变化时报告具体来源和版本；沿用原 seed 续跑。
