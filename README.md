# 户外广告投放履约采录台

离线采录广告素材审查、屏位时段占用和第三方监播证据。素材和地域资质决定排期资格；维护及独占冲突改变屏位承诺；有效监播分钟而非计划分钟决定欠播与补投。来源文件原字节及 SHA256、各子系统独立状态、排期/播放记录和义务均在 SQLite 留痕。所有城市、机构和广告均为合成数据。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m adflight demo --db demo.sqlite
python -m adflight audit --db demo.sqlite
python -m pytest -q
```

CLI 入口：`creative-import`、`license-import`、`screen-import`、`maintenance-import`、`booking-import`、`cancel-import`、`proof-import`、`makegood-import`，形式为 `file --db state.sqlite`；`settle --db state.sqlite --booking B1` 计算欠播；`report` 和 `audit` 查询与重放。正常/失败样例在 `fixtures`。

车载屏市政路段许可：`gps-import`、`calibration-import`、`map-import`、`camera-import`、`adjudication-import`、`decision-import`、`stop-import` 采录原始 GPS/惯导帧、共享基站偏差校准证、版本化道路拓扑和路口独立摄像机经过证据；`route-report --db state.sqlite --package P1` 输出满足全部原始帧的全程候选轨迹、逐段许可裁决（必在许可区/必在禁播带/两者皆可能/缺证）、共同安全区、最近路基线的错误放行对照、停屏指令与许可当前/历史版本；`route-demo` 在同一原始定位包上演示避让经营（路口摄像机补证后许可持续）与暂停核验（硬件停屏回执 + 补播权益）两条合法处置。候选轨迹必须在车速、单行路和时间窗上全程路网可达；决策必须在路口前固定，地图修订只重裁受影响时段，执法回执保留原版本。

首版只做本地模拟的审核主体和监播来源标签；不接真实广告平台、摄像头和资质签章。有效分钟按独立、不重叠且落在已确认排期内的片段累计。
