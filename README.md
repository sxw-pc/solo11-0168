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

## 广告主证据包发布协议

`publish` 产出的是不可变快照包：`packages/<id>/` 下固定 `body.json`（booking/proof/obligation 版本）、`proof-index.json`（原始 proof 索引）、`settlement.json`（结算摘要与义务台账）和 `manifest.json`（接收人可见清单与逐文件 SHA256）。打包前先固定 booking/obligation 版本，写盘期间 report 变化不会混入半旧半新。同 booking 的发布路线在首次发布前固定：`closeout`（封账验收，期间定稿后拒绝再发新版）或 `rolling`（截止前可用新包撤回当前指针）。

文件先写入 `.staging/<id>/` 并逐文件 fsync，再原子 rename 到 `packages/<id>/`，最后才提交当前指针；指针只指向已完整落盘且校验通过的版本。`package-recover` 在每个崩溃点重启后把包归入四类之一：`never_externally_visible`（暂存未外发，可中止或续发）、`published_pending`（已公开待确认）、`client_accepted_old`（客户已签收旧版，字节保持可追溯）、`manual_review`（磁盘与台账不符，只隔离不删除、不覆盖）。

`package-claim` 记录领取时刻并校验磁盘字节；`client-ack` 比对的是客户实际领到的清单与内容摘要（manifest digest + 逐文件哈希），而非仅数据库 digest，文件被替换或缺失时回执置 `held` 并把包 `quarantined`。包按 booking/recipient 隔离，广告主 A 的包不会夹带广告主 B 的素材。`publication-report` 区分“平台认为已发”与“客户确实验收”，`publication-audit` 用磁盘实际字节重放校验。崩溃注入用发布请求里的 `crash_at`（`after_stage_write` / `after_staging_commit` / `after_rename`），样例见 `fixtures/publish-crash-*.json` 与 `tests/test_publication_recovery.py`。
