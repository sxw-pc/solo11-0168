# 户外广告投放履约采录台

离线采录广告素材审查、屏位时段占用和第三方监播证据。素材和地域资质决定排期资格；维护及独占冲突改变屏位承诺；有效监播分钟而非计划分钟决定欠播与补投。来源文件原字节及 SHA256、各子系统独立状态、排期/播放记录和义务均在 SQLite 留痕。所有城市、机构和广告均为合成数据。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m adflight demo --db demo.sqlite
python -m adflight audit --db demo.sqlite
python -m pytest -q
```

CLI 入口：`creative-import`、`license-import`、`screen-import`、`maintenance-import`、`booking-import`、`cancel-import`、`proof-import`、`makegood-import`，形式为 `file --db state.sqlite`；`settle --db state.sqlite --booking B1` 计算欠播；`report` 和 `audit` 查询与重放。正常/失败样例在 `fixtures`。

## 离线维保与复役

黑屏采录包（`capture-import`，fixtures 在 `fixtures/repair/`）为每条证据（仪表波形、面板自检、心跳、备件扫码、复验照片、独立电源隔离读数、面板试亮）保留原件哈希身份、设备时钟校准偏差和操作人；一份“心跳恢复”不能证明内容可安全上屏。首次黑屏采录对每台屏同时保留供电/面板两种候选故障，并打开开放式停电窗（`maintenance.end_at` 为空），重叠的 confirmed 预约裁定为 interrupted，过去欠播义务不因复役改变。

同一采录包与两个完整物理世界相容：`W-PSU`（S1 供电、S2 面板）与 `W-PANEL`（S1 面板、S2 供电）。仅凭总线电压跌落和不完整自检不能区分；补采的独立电源隔离读数与面板试亮（`diagnosis-close`）把可能世界缩到一个，并指出缺哪项复验结论仍不可靠。

- `naive-dispatch` 演示逐屏本地阈值方案：两台屏都被派给唯一电源模块（首文件抢占、第二台搁浅）、忽视技师跨场 4 分钟移动窗，并在至少一个物理世界错修另一台。
- `plan-import` 的两条事前授权路线在诊断未区分前固定目标与客户承诺：`fast_core` 先给核心场地 S1 配整机（10:08 后可承接新订单，S2 暂停）；`min_downtime` 按确证世界把唯一模块给供电故障屏、整机随后到另一屏。诊断闭合后才追认的计划被拒（`closure_before_lock`）。
- `repair-execute` 校验技师到达窗、停电许可、备件预留与跨场移动；新件安装与旧件返库是不同物理序列号的独立台账（同序列号替换被拒）。
- `recommission-import` 只有在供电隔离与面板试亮两项独立复验都通过时才关闭停电窗、释放未来售卖窗；仅心跳或缺任一检查保持 `suspended`，复役时间不得早于停电开始（禁止倒填已卖时段）。

首版只做本地模拟的审核主体和监播来源标签；不接真实广告平台、摄像头和资质签章。有效分钟按独立、不重叠且落在已确认排期内的片段累计。
