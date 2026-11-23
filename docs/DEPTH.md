# 深度登记 水产拍卖结算

规模：中型。使用者：水产批发市场结算员。核心规则：先毛皮配对，再扣杂计价，按更正链选择唯一生效版，只支付差额。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 称重采信 | 查看并执行称重采信 | `POST /api/intake` | `app/modules/intake/rules.py` | `tests/test_intake.py` |
| M2 货品换算 | 查看并执行货品换算 | `POST /api/conversion` | `app/modules/conversion/rules.py` | `tests/test_conversion.py` |
| M3 毛皮配对 | 查看并执行毛皮配对 | `POST /api/pairing` | `app/modules/pairing/rules.py` | `tests/test_pairing.py` |
| M4 扣杂竞价 | 查看并执行扣杂竞价 | `POST /api/auction` | `app/modules/auction/rules.py` | `tests/test_auction.py` |
| M5 更正裁决 | 查看并执行更正裁决 | `POST /api/correction` | `app/modules/correction/rules.py` | `tests/test_correction.py` |
| M6 应付对账 | 查看并执行应付对账 | `POST /api/reconcile` | `app/modules/reconcile/rules.py` | `tests/test_reconcile.py` |
| M7 结算归档 | 查看并执行结算归档 | `POST /api/archive` | `app/modules/archive/rules.py` | `tests/test_archive.py` |
| M8 召回处置 | 核对来源批/买家/生效版本并发布处置单 | `POST /api/recall`、`POST /api/recall/{run_id}/publish` | `app/modules/recall/rules.py` | `tests/test_recall.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码中文票据 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_legacy_encoding` |
| TRAP-02 | BOM表头 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bom_real` |
| TRAP-04 | 分段表头 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_repeated_header_real` |
| TRAP-05 | 缺列记录 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bad_columns_real` |
| TRAP-07 | 空值哨兵 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/conversion/rules.py` | `tests/test_conversion.py::test_sentinel_real` |
| TRAP-08 | 前导零票号 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_leading_zero_real` |
| TRAP-14 | 重复编号不同原值 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_duplicate_conflict_real` |
| TRAP-18 | 三类空态 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_empty_states` |
| TRAP-15 | 吨与千克混合 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/conversion/rules.py` | `tests/test_conversion.py::test_tonnes_counterexample` |
| TRAP-06 | 全角千分位 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/conversion/rules.py` | `tests/test_conversion.py::test_fullwidth_thousands` |
| TRAP-10 | 金额半分舍入 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/auction/rules.py` | `tests/test_auction.py::test_half_cent_rounding` |
| TRAP-26 | 陈旧更正分支 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/correction/rules.py` | `tests/test_correction.py::test_stale_branch` |
| TRAP-31 | 抽检迟到更正版本 | 按出票时点放行已售票 | 按生效报告版本裁决，已售票仍召回 | `app/modules/recall/rules.py` | `tests/test_recall.py::test_effective_report_uses_late_correction` |
| TRAP-32 | 来源批/买家去向追不到 | 缺证据却判安全 | 一律暂缓召回，禁止免召回 | `app/modules/recall/rules.py` | `tests/test_recall.py::test_untraced_source_batch_never_safe` |
| TRAP-33 | 船次相邻来源批 | 全船扣住或直接放行二选一 | 先船次扣住，逐票核清且生效合格后解除 | `app/modules/recall/rules.py` | `tests/test_recall.py::test_voyage_control_holds_then_releases_neighbor_batch` |
| TRAP-34 | 发布中断/重复发布 | 半成品单据或重复召回单 | 同事务原子发布，order_no 幂等 | `app/store.py` | `tests/test_recall.py::test_publish_interruption_keeps_previous_version` |

## 召回裁决规则

三类证据（抽检报告 `recall_inspections.csv`、成交 `recall_sales.csv`、去向 `recall_destinations.csv`）原始字节随批次快照持久化，封存 zip 内原样保留，任何隔离行都带原始值，不发生数据丢失。

- 抽检报告按来源批走更正链：后续版本只能替代当前生效版，缺基版或陈旧分支整批阻断，关联已售票判 `暂缓召回/REPORT_BRANCH_BLOCKED`，不判安全。
- 已售票按来源批的**生效版本**裁决，不按出票时点放行；迟到更正改变结论或不合格项时，已售票仍进入召回范围。
- 票级结果三档：`召回`（不合格且去向核清，已召回/待召回分别记账）、`暂缓召回`（来源批无证据、报告断链、无去向、买家失联或买家对不上）、`免召回`（生效版全部合格）。追不到来源批或买家去向的票永远不会进入免召回。
- 船次控制两档：同船存在不合格生效批时，不合格批全批控制，相邻合格批先 `控制-相邻批暂缓`，仅当该批全部已售票均免召回且每张票去向登记核清后才 `解除-核清相邻批`。
- 处置单含证据级阻断（隔离行或报告冲突）时状态为 `待发布`，发布接口 422 拒绝；无阻断为 `可发布`。发布按 `order_no` 幂等，乐观锁校验 revision，旧版本提交 409；发布中断随事务回滚，只保留旧版。
- 同输入（含三类证据字节）重跑生成相同 run_id、order_no 与逐票结果；证据行乱序只影响溯源行号，不影响裁决语义。

## 对账规则

输入=采纳原始行+隔离行+空行+同值重复行+重复表头。计价版本总额与生效票总额分别展示，避免把更正前后重复求和。最终应付=已付+待付，负待付为退款。

## 已知不覆盖

无称重设备实时接入，采用现场导出文件。无支付执行，只生成可复核结算证据。
