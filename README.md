# 户外广告投放履约采录台

离线采录广告素材审查、屏位时段占用和第三方监播证据。素材和地域资质决定排期资格；维护及独占冲突改变屏位承诺；有效监播分钟而非计划分钟决定欠播与补投。来源文件原字节及 SHA256、各子系统独立状态、排期/播放记录和义务均在 SQLite 留痕。所有城市、机构和广告均为合成数据。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m adflight demo --db demo.sqlite
python -m adflight audit --db demo.sqlite
python -m pytest -q
```

CLI 入口：`creative-import`、`license-import`、`screen-import`、`maintenance-import`、`booking-import`、`cancel-import`、`proof-import`、`makegood-import`，形式为 `file --db state.sqlite`；`settle --db state.sqlite --booking B1` 计算欠播；`report` 和 `audit` 查询与重放。正常/失败样例在 `fixtures`。

竞价采录：`auction-ingest page.json --db state.sqlite` 分页采录门户密封包价（分页可乱序，水位不齐拒绝分配）；`auction-allocate --db state.sqlite --auction AUC-1` 按事前公示目标（`max_revenue` 总收益优先 / `public_welfare_first` 公益保底且剩余收益最大）预览可重算的中标与未中标理由；`auction-publish --db state.sqlite --auction AUC-1` 原子提交分配并创建全部预约；`auction-correct correction.json --db state.sqlite` 把截止后的更正独立留痕而不改已公示出价。包价支持 `all_or_nothing`（全中或全失）与 `one_of`（择一屏位）；创意许可、维修窗与既有确认预约先核验再联立求解，同一屏同一秒不会卖给两个包。发布前屏故障按包承诺重裁，发布后故障走 maintenance→obligation 补救，绝不重开竞价。

首版只做本地模拟的审核主体和监播来源标签；不接真实广告平台、摄像头和资质签章。有效分钟按独立、不重叠且落在已确认排期内的片段累计。
