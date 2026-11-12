# 01-feedstitch（信汇）

Feedstitch — 多源目录/价目 Feed 归并落库：解析 → 业务键去重/冲突隔离 → 按源水位线增量或全量落库。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5100 |
| API | http://localhost:10100 |

## 主链

配置源与夹具 → 触发 Run → 解析 / 去重 / 冲突隔离 → Canonical + 运行报告对账

## 种子说明

- **Alpha Catalog（clean）**：JSON，三件干净货号，增量可落库。
- **Beta Offers（dirty）**：JSONL，含与 Alpha 同键但价格不同的冲突行，以及迟到低 revision 行（底座增量会因水位线跳过）。
- **Gamma Shelf（xml）**：简易 XML，两件独立货号。

打开“运行台”选源跑一次增量，再看 Staging / 冲突 / Canonical 三页计数是否与本次 Run 报告一致。

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 水位提交闸（watermark_commit_gate，已实现）

每次 Run 处理完暂存 / 主数据 / 冲突后、提交水位前先过提交闸：

- 对账内容：报告 parsed/upserted/conflict/skipped 计数 vs 本次 Run 的 staging 处置行数；conflicts 表行数；Canonical 条数变化 vs `insert` 处置数；增量水位单调不回退。
- **闸通过**：才把该源水位更新为本次候选水位，Run 状态 `ok`。
- **闸失败**：保持跑前水位不变，Run 状态 `gate_failed`（处理完成但对账未通过 / 水位未提交），失败原因写入运行报告 `report.gate.checks`，可经 `/api/runs/{id}` 查询。
- 水位线页（`/api/watermarks`）展示当前水位、最近一次成功提交与最近闸检结果，并校验二者一致；运行台展示每次 Run 的闸检明细，可与 Staging / 冲突 / Canonical 列表页对照（数字同源）。
- 演示闸失败：在设置页把 `gate_fault_injection` 改为 `drop_staging_row`（丢暂存行）/ `skew_report_counter`（计数偏移）/ `canonical_drift`（主数据幽灵行）之一再触发 Run；改回 `off` 恢复。

## 0-1 切口（未实现）

`lookback_backfill` / `schema_drift_guard` / `dead_letter_replay` / `webhook_push_source` /
`source_authority_rank` / `tombstone_retire` / `scd2_history` /
`field_map_transform` / `lineage_provenance`
