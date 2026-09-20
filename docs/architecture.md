# 系统架构与 API

## 模块

```
src/museum_merch/
├── schema.sql     全部表结构（约 25 张表）
├── store.py       SQLite 连接、显式事务（BEGIN IMMEDIATE + 进程内可重入锁）
├── rights.py      展览/权利人/作品/授权登记、许可匹配、准入闸门
├── catalog.py     产品、版本、计划市场、渠道额度、内容审核、上架/恢复
├── fulfillment.py 采购单、生产批次、质检、库存台账、订单/预留/发货/取消/退货
├── royalty.py     版税权责发生、退款冲回、按权利人结算
├── cases.py       事件处置引擎：事件→工单→动作；到期巡检；替代裁决
├── analytics.py   风险看板、库存敞口、待续权利、全链路追溯
├── seed.py        演示数据
└── api.py         标准库 http.server 实现的 JSON REST API
```

### 数据主链

`exhibition → artwork（→ holder_split）→ license（use/channel/region/evidence）`
`→ product_version（→ market / content_review / gate_review / listing）`
`→ purchase_order（committed 承诺）→ production_batch → quality_check → stock_ledger`
`→ sales_order/order_item → stock_reservation → shipment → royalty_accrual → settlement`
横切：`domain_event → case_record → case_action`。

## 启动

```bash
PYTHONPATH=src DATABASE_PATH=museum.db DATABASE_SEED=1 \
  python -m museum_merch.main        # 默认 :8080
```

## API 速览

所有时间用请求体 `on` 或查询参数 `on`（YYYY-MM-DD）注入，便于推演「到期日当天」。

### 策展与授权
- `POST /api/exhibitions`，`POST /api/exhibitions/{id}/reschedule`
- `POST /api/holders`，`POST /api/artworks`
- `POST /api/licenses`（uses/channels/regions/期限/凭证）
- `POST /api/licenses/renew`（复制旧授权形成续展链）
- `POST /api/licenses/{id}/withdraw`（立即触发处置工单）
- `POST /api/expiries/sweep`（到期巡检：到期授权自动停售退款）

### 产品与闸门
- `POST /api/suppliers`，`POST /api/products`，`POST /api/products/{id}/versions`
- `POST /api/versions/{id}/markets`，`POST /api/versions/{id}/quotas`
- `POST /api/versions/{id}/content-review`
- `GET  /api/versions/{id}/gate?stage=sale&channel=..&region=..&on=..`
- `POST /api/versions/{id}/listings`，`POST /api/versions/{id}/resume`

### 采购、生产、质检
- `POST /api/pos`（强制采购闸门），`POST /api/pos/{id}/confirm`
- `POST /api/pos/{id}/delay`（延期，触发工单）
- `POST /api/pos/{id}/batches`，`POST /api/batches/{id}/qc`（不合格触发工单）

### 订单履约
- `POST /api/orders`（多明细、共享库存、渠道额度校验）
- `POST /api/order-items/{id}/ship`（部分发货 + 版税计提，FIFO 批次）
- `POST /api/order-items/{id}/cancel`（取消未发货，释放预留并退款）
- `POST /api/order-items/{id}/return`（已发货退货，冲回版税，可选回库）

### 工单与版税
- `GET  /api/cases`，`GET /api/cases/{id}`（含每个动作的状态与解释）
- `POST /api/cases/{id}/substitute`（确认替代版本）
- `POST /api/cases/{id}/decline-substitution`（不替代→挂起订单退款）
- `POST /api/case-actions/{id}/resolve`（续展/重订等人工动作线下完成后销项）
- `POST /api/royalties/settle`

### 看板与追溯
- `GET /api/renewals?on=..&within=60`（待续权利：到期窗口 × 在售/在产依赖）
- `GET /api/exhibitions/{id}/risk?on=..`（临展风险：权利缺口、库存敞口、工单、未发货）
- `GET /api/stock/exposure`（全部版本的库存/在途/能否销售/敞口货值）
- `GET /api/versions/{id}/stock`，`GET /api/versions/{id}/trace`（全链路）
- `GET /health`

错误统一为 `{error, message, detail?}`，常见码：`gate-failed/422`、
`oversell/409`、`rights-lapsed/422`、`invalid-state/409`、`not-found/404`。

## 典型推演

```bash
# 1. 授权撤回：自动停售三渠道、退未发货款、停产保留 PO；快闪有独立授权不受影响
curl -X POST .../api/licenses/lic-ink41-main/withdraw -d '{"reason":"家属撤回","on":"2026-09-20"}'
# 2. 续展后恢复
curl -X POST .../api/licenses/renew -d '{"code":"L1-2027","old_license_id":"lic1",...}'
curl -X POST .../api/versions/v1/resume -d '{"markets":[{"channel":"museum-store","region":"CN"}],"on":"2026-09-20"}'
# 3. 到期巡检（日常定时任务）
curl -X POST .../api/expiries/sweep -d '{"on":"2026-11-01"}'
```

## 并发与一致性

SQLite 单库：写事务全部 `BEGIN IMMEDIATE` 并由 Store 内一把可重入锁串行化，
读方法在锁内执行并可在事务中嵌套。下单失败整单回滚；进程级扩展时可把
`Store` 换成 Postgres + `SELECT ... FOR UPDATE`，领域逻辑不变。
