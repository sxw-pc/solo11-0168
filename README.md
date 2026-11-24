# 户外广告投放履约采录台

离线采录广告素材审查、屏位时段占用和第三方监播证据。素材和地域资质决定排期资格；维护及独占冲突改变屏位承诺；有效监播分钟而非计划分钟决定欠播与补投。来源文件原字节及 SHA256、各子系统独立状态、排期/播放记录和义务均在 SQLite 留痕。所有城市、机构和广告均为合成数据。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m adflight demo --db demo.sqlite
python -m adflight audit --db demo.sqlite
python -m pytest -q
```

CLI 入口：`creative-import`、`license-import`、`screen-import`、`maintenance-import`、`booking-import`、`cancel-import`、`proof-import`、`makegood-import`，形式为 `file --db state.sqlite`；`settle --db state.sqlite --booking B1` 计算欠播；`report` 和 `audit` 查询与重放。正常/失败样例在 `fixtures`。

## 面板采录与颜色版本校验

相机 sRGB 不是人眼现场颜色：相机滤色片与 CIE 观察者是同一发光谱上的两组不同线性算子，存在相机同值、人眼异值的零空间（同色异谱）。在相机数据上拟合 3×3 LUT、最小二乘或多拍色卡都不能消除未观测光谱的零空间，再加一个固定第四探头也只在四维玩具上有效——实际屏面随温度漂移的窄峰可落在固定滤波带之间。颜色证据因此建立在**光谱**上：

- `calibration-import` 登记仪器校准证（有效期、签发机构、序列号）；`panel-import` 登记屏机面板版本与发光体模型、热漂移界、量化位数、温度/驱动包络；`capture-import` 留存每次离线采录的相机 RGB、四波段读数、全谱光谱仪读数、面板温度、驱动电流、环境照度、样本位置与测量时钟，光谱仪校准过期即拒收。
- `color-target-import` 登记每个 creative revision 的原色/备用双色目标与合同容差；`color-approval` 对面板版本签发可解释证书。原色路线要求全谱仪器当前校准有效、温度×驱动包络每个单元都有实测覆盖、每个采录在仪器误差预算内的人眼三刺激包围盒落入容差，并生成**硬件可实现**的 LUT：超色域高亮目标只能降低实际亮度（记录 luminance_scale），驱动量化、热漂移或色相不可达时拒绝，绝不输出数学上好看但面板发不出的 RGB。备用路线是已批准的黑白品牌版，屏机保留全亮度，独立登记，不能记成原色已履行。
- 预约确认前必须固定 `color_route`（primary/fallback），且该 revision 在该屏面板上有对应、未撤销的批准。`color-revoke` 在光谱仪复校推翻批准时，先冻结受影响的尚未播放预约（`color_frozen`）并向广告主发出通知；冻结后原色路线不可再预约，备用路线仍可合法交片。

放行/补采/拒绝的判据是**对所有与读数、厂商光谱支持/漂移界和仪器误差相容的光谱**求证人眼色差上界：发现厂商支持却无仪器覆盖的波段时判 `resample`，而不是把未测波段设为零；采样间距粗于漂移界时采样计划直接判仪器能力不足。四维反例的完整数值见证在 `fixtures/color/toy-witness.json`，窄峰反例在 `narrow-peak.json`，合成仪器/面板/LUT fixture 由 `tests/build_color_fixtures.py` 用同一前向模型生成，可随时重建。

首版只做本地模拟的审核主体和监播来源标签；不接真实广告平台、摄像头和资质签章。有效分钟按独立、不重叠且落在已确认排期内的片段累计。所有光谱、观察者函数和面板参数均为合成数据。
