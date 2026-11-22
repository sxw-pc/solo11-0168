# 户外广告投放履约采录台

离线采录广告素材审查、屏位时段占用和第三方监播证据。素材和地域资质决定排期资格；维护及独占冲突改变屏位承诺；有效监播分钟而非计划分钟决定欠播与补投。来源文件原字节及 SHA256、各子系统独立状态、排期/播放记录和义务均在 SQLite 留痕。所有城市、机构和广告均为合成数据。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m adflight demo --db demo.sqlite
python -m adflight audit --db demo.sqlite
python -m pytest -q
```

CLI 入口：`creative-import`、`license-import`、`screen-import`、`maintenance-import`、`booking-import`、`cancel-import`、`proof-import`、`makegood-import`，形式为 `file --db state.sqlite`；`settle --db state.sqlite --booking B1` 计算欠播；`report` 和 `audit` 查询与重放。正常/失败样例在 `fixtures`。

首版只做本地模拟的审核主体和监播来源标签；不接真实广告平台、摄像头和资质签章。有效分钟按独立、不重叠且落在已确认排期内的片段累计。

## 匿名客流采录与可证触达人数

`audience-import` 保留原本地 token 台账：仍要求 `booking.status=confirmed`，结果只是 `COUNT(DISTINCT token)`——换码设备的 token 数不等于实际人数。新增的有界归属层另走一组原件，原始 token 只存本地、不出报告：

- `probe-import`：探针分页原始帧（含分页是否闭合），`gate-count-import`：单向门机械过门计数分页，`calibration-import`：传感器时钟校准区间与共享时钟组、断线区间，`geometry-import`：单向门方向、可视区域与两门最短步行时间。
- 归属引擎把每条过门约束放进**同一个**差分约束系统：每个时钟组只有一个共享偏差变量，不能逐帧各取极值；再枚举与全部原件相容的完整见证世界。同一预约窗口据此给出可证的最小/最大独立到访人数，以及取到两端的见证分配（仅含帧 id 与门路径）。换码重复帧不增加人，不可达时间内的异门过门不能由同一人解释，严格单向门禁止同一人重复过门。
- 覆盖水位：探针分页未闭合、设备断线或门计数缺页时上界报未知（`max_persons=null`），绝不把未见行人计成零；门计数中超出设备帧的过门只计入上界余量。
- `contract-import` 登记采录前签署的收费路线。保证触达按可证下界结算（下界 2 < 门槛 3 即不付达量奖金，报告“2–3，未保证达量”）；机会触达按可行上界结算，付款报告必须标注“仅可能触达、非确认三人”，上界未知时不计费。
- `bridge-import` 接入后补可验证 opt-in 人员桥接：界与未付款项按新版本重算，已真实出账的奖金保留旧版依据并写 `correction` 更正记录，不改写历史。
- `audience-bounds --db state.sqlite --booking B1` 输出界、两端见证与结算；`audit` 对全部原件（含新证据类型）重放并重建派生行，校验逐字节一致。
