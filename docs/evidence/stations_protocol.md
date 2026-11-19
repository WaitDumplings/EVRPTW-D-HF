# 开发城市充电站预算配对比较

使用 Philadelphia、Fort Worth、Chicago 的本地冻结 r1 输入。远端客户和预算都在读取选站结果前定义。四档沿用同一城市的三个仓库，仓库来自不同 Census block group。先保留四档均有足够独立种子的 r1 仓库，取最靠近这些仓库平均坐标的一个，再依次取距已选仓库最近距离最大的两个。每仓库和规模按原始 draw 顺序取前几份成功客户足迹。Cus100 取三份，其余三档各取两份。对每份足迹从所属城市的全站池运行一次贪心选站，按同一顺序截取不同预算。

| 客户数 | 每城足迹数 | 配对站数 | 试验前暂定预算 |
|---:|---:|---|---:|
| 100 | 9 | 5、10、20 | 5 |
| 500 | 6 | 20、30、40 | 30 |
| 1000 | 6 | 20、30、40 | 30 |
| 2000 | 6 | 40、50、60 | 50 |

客户足迹取自已存档的开发城市抽样。该批足迹用历史道路上的 tract 准入产生，每份客户清单以 r1 的住宅轮廓和道路接入重新绑定。接入失败地点从原 tract 的正户数住宅按条件 Bernoulli 补齐，逐例保存替换数和最终地点 ID。每份足迹内的三个预算共享全部客户、depot、物理候选站和工作日道路成本。本次比较评价选站，不含订单时间窗、全天路线或神经模型训练。

## 计量

- 以 depot 和所有客户为顶点，双向最短道路距离的算术平均为边权建最小生成树。每条树边两个方向分别计分。一个方向的联合成本等于绕行公里数与绕行行驶时间加补电时间两项全池极差归一化值的均值。补电量取该方向起点到站的耗电量，使用站点有效功率。
- 远端定义为每例按 depot 出发有向道路公里数最远的 10% 客户。统计从这些客户出发的有向树边。全池最佳保留率按联合成本比较，允许同分替代。另对每名客户到最近的非树邻居测留出边覆盖率，同时对仍使用原选择顺序的固定补 20 kWh 情形重算全树覆盖率。
- 每例先算保留率，再在实例间等权平均。另给出有遗漏的足迹数、方向数及受影响远端客户数。Cus100 漏选方向同时记录全池最优站、预算内最优站、额外道路公里和额外行驶加补电分钟。两个分量均为有符号差值，以保留更近但更慢等取舍。
- 全池联合成本是选站层面的参照。与全池一致的方向可能在实际路线中不充电，某条方向存在差值也不能推出整条路线会变差。

## 复算

使用解包的 Stage 1 r1 代码及各城的 roads、speeds、depots、charging、customer_inputs/geometry。在本仓库用 maojie Python 环境运行下列命令。输出中每个城市的 plan 列出预选 depot、BG、draw 和 seed，逐例 JSON 包含最终客户 ID、选站顺序和逐预算结果。每份足迹独立落盘。已有同一预选清单的输出会被复用，独立复算时指定新的输出目录。

```sh
python analysis/charging_budget_multicity/run.py "$R1_ROOT" philadelphia --scratch "$LOCAL_SCRATCH"
python analysis/charging_budget_multicity/run.py "$R1_ROOT" fort-worth --scratch "$LOCAL_SCRATCH"
python analysis/charging_budget_multicity/run.py "$R1_ROOT" chicago --scratch "$LOCAL_SCRATCH"
python analysis/charging_budget_multicity/summarize.py
python analysis/charging_budget_multicity/plot.py
```

此次本地运行按需恢复了三个城市的 r1 组件，核对了道路源文件的 SHA-256、可用仓库数和可用物理站数。二进制表采用本地重建版本。input_index.json 同时保留本地组件与原回执的摘要。临时目录中的部分 GraphML 文件在完成计算后做了无损 gzip 压缩，从临时目录重新绑定新端点时应先解压。工作日最短路矩阵按来源行计算并逐例抽行对照原生路由器。此前 Dallas 四档的选站顺序和四项覆盖指标已逐项复现。

## 多片区 Cus100 补充对照

主样本的 27 份 Cus100 全部只有一个地理连通片区。在三城原有的 720 份 Cus100 足迹中，选出全部 K_geo 大于 1 的足迹，逐一检查原 depot 是否仍在 r1 可用仓库表中。25 份中有 23 份可绑定，另两份保留在 disconnected/eligibility.csv。对这 23 份采用完全相同的客户接路、站点选择和比较指标。它们单独报告，用来检验相互分离的片区，不计入主样本平均。复算时为 run.py 和 summarize.py 增加 --disconnected。
