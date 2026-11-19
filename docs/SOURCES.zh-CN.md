# 构造输入

`python pipeline.py --work "$WORK" sources` 直接访问数据发布方。各机把响应缓存在 `raw/`，把处理结果写在 `public/`。可以按需要分别运行 `sources --only stage1`、`sources --only stage2`，或先用 `sources --only cities --city chicago` 检查一个城市的空间处理。

| 来源 | 服务器下载 | 本地处理 |
|---|---|---|
| [Census TIGER/Line 2025](https://www2.census.gov/geo/tiger/TIGER2025/) | 全国 COUNTY、各州 PLACE 和 BG、市域相交各县的 AREAWATER ZIP | 按 place GEOID 选市界，扣除水面。采用完整原生 BG；按 tract 融合，正长度共边构成 frontier 邻接 |
| [Microsoft GlobalMLBuildingFootprints](https://github.com/microsoft/GlobalMLBuildingFootprints) | 2026-08-13 索引中覆盖 11 城的 29 个原始压缩瓦片，逐瓦片的官方 URL、quadkey 和 SHA-256 随代码固定 | 按原始轮廓的代表点选入市域，过滤无效及精确重复的轮廓，保留地点标识 |
| [OpenStreetMap / Geofabrik](https://download.geofabrik.de/north-america/us.html) | 州级 PBF，南加州使用 California/Southern California 子区。默认 2026-11-01，也可指定其他 `YYYY-MM-DD` 发布日 | 从同一份 PBF 提取建筑标签、仓库候选，以及与市界外扩 20 km 区域相交的完整道路。靠近州界时，道路覆盖受该 Geofabrik 提取区边界限制。保留道路引用的禁行／屏障节点，记录 PBF 哈希与复制时间。构造有向道路和逐日型速度 |
| [USACE National Structure Inventory](https://www.hec.usace.army.mil/confluence/nsi/technicalreferences/latest/api-reference-guide) | 逐县请求公开 `structures?fips=<county>&fmt=fs`，按 GeoJSON Text Sequence 读取 | 按陆域筛选原生点，按 fd_id 核对重复，保留住宅、非住宅、零户及缺失证据。接入建筑匹配和 11 城住宅户数校准 |
| [AFDC / National Laboratory of the Rockies](https://developer.nlr.gov/docs/transportation/alt-fuel-stations-v1/) | 美国在运营、公众可用的电站 CSV 和同范围 EV charging units CSV | 绑定地点和设备，按当前下载的全国兼容端口重算缺功率时使用的加权中位数。地址级地点按 Census 核对，车辆兼容性和功率上限沿用 Stage 1 方法 |
| [Census Geocoder](https://geocoding.geo.census.gov/geocoder/) | 将相关 AFDC 公共站的地址按批提交至官方地址匹配接口，接收地址匹配结果 | 对照回传的原地址和坐标；已具原生 GPS 的站使用 AFDC 位置 |
| [Amazon Last Mile Routing Research Challenge](https://registry.opendata.aws/amazon-last-mile-challenges/) | 开放 S3 桶中官方 training 的 `route_data.json` 和 `package_data.json`，匿名 HTTPS 下载 | 重建有效整单、包裹、计划服务秒数及地址时间窗，按 donor ID 联合使用。校验生成器读取的字段和行顺序，允许不同平台使用等价的 Parquet 压缩编码 |
| [EPA 2023 NEI onroad](https://gaftp.epa.gov/Air/nei/2023/doc/TSD/onroadmobile-agencymovesinputs.html) | 生成阶段使用随代码版本固定的 7 州速度参数。州配置列出 EPA 统计源及推导口径 | Stage 1 将标定的速度比例用于实际道路。可运行 `PYTHONPATH=stage1_engine/src python stage1_engine/scripts/verify_state_speed_profiles.py` 从紧凑统计表复核系数 |

客户起始组数的拟合系数、逐城住户概率、候选预算比例的经验分布、车辆和道路速度标定属于模型参数。仓库中 `config/candidate_ratio.csv` 只保留采样所需的经验原子标识、比例和累积概率。下载和运行无需项目方的输入压缩包、已有 CLE、实例或私有 API。

**公开可访问和用途许可。** Amazon 数据使用 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)；需要允许商业用途的数据白名单时，应先确认该来源是否符合服务器规定。OSM 应保留[署名和 ODbL 条款](https://www.openstreetmap.org/copyright)。Microsoft 轮廓及其余数据采用来源方发布的条款。

**来源锁。** `public/sources.lock.json` 记录每次实际使用的官方 URL、原始响应字节数和 SHA-256，不含 API 密钥。多机复现时使用同一 Git 提交、同一 OSM 日期和第一台机器的来源锁。相同下载内容可在组织内部的原始缓存中复用。初次获取的数据版本和冻结 r1 的城市数量统计分开汇报。

多机先同步运行 `sources --only live`，再比对 `live.lock.json`。AFDC 的实时端点会更新。官方另提供按日的历史站点和设备导出，需要填写联系信息并接受下载条款，使用的文件格式也与实时 CSV 不同，本版自动入口使用实时 API。同日采集也以哈希比对为准。历史可用范围见[AFDC 下载](https://afdc.energy.gov/data_download/new)和[历史设备字段](https://afdc.energy.gov/data_download/historical_stations_format)。
