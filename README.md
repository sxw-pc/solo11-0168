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

## 能耗采录与尖峰控制

离线电表/UPS/控制回执命令：`meter-import`（总表/分表/UPS 逐秒有功无功，`segments` 展开为逐秒行）、`calibration-import`（各表共用的校验证书，单一相对偏差）、`curve-import`（屏机亮度档→耗电区间曲线）、`brightness-import`（驱动器亮度档记录）、`hvac-import`（空调压缩机候选循环）、`load-import`（商户照明等恒定负载）、`clock-import`（场馆时钟证书）、`ups-import`（电池 SOC/逆变效率/最大放电功率快照）、`control-import`（尖峰前固定的 `dim`/`discharge` 控制令）、`receipt-import`（屏控回执）。查询：`attribute --db D --start S --end E` 给出断采期间各屏与场馆的可证能耗区间、全部相容世界（W-low/W-high）的完整功率流与守恒见证；`energy-report` 另含峰值承诺、UPS 储能与广告主补救权益。

归属在读数空间逐秒守恒，真值包络由同一个共用校准偏差统一缩放（不逐秒取极值）；峰值承诺在所有与采录相容的分配下成立；控制令提交前把电池 SOC、逆变效率、亮度耗电曲线与有效预约快照进 `basis`；同一控制令重发按来源 SHA 幂等，累计放电受逆变器功率与可用能量双重约束（防过放）；广告主的降档时段与补救权益只来自屏控回执，不从用电反推画面。
