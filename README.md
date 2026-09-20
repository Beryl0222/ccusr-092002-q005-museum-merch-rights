# 美术馆文创授权履约系统

公立美术馆特展周期短（通常两三个月），文创开发却要同时向艺术家、家属与合作机构确认
不同用途的权利，供应商又要求提前锁定产能。本系统把**策展 → 授权 → 打样 → 生产 → 销售**
贯穿为一条带门禁的链路，将每件作品在**复制、改编、宣传、地域、渠道、期限**上的许可边界
与证据文件关联，并在特展改期、授权撤回/到期、供应商延期、质检不合格时触发可解释的
停产、停售、替代或退款流程。

## 核心规则

1. **许可六维度 + 证据**：每条许可记录用途（`exhibition-promotion` 宣传 /
   `product-reproduction` 商品复制 / `product-adaptation` 改编）、渠道、地域、起止日期、
   可否转授权、版税条款；没有证据文件（合同/邮件/书面同意）的许可不产生效力。
2. **用途互不替代**：宣传图获准不等于商品可以复制；改编产品须同时具备复制权与改编权。
3. **双重门禁**：
   - 采购门禁：锁定供应商产能前，所需用途的许可必须全部有效、有证据，内容审核通过；
     可按目标渠道/地域预检，避免为无权渠道备货。
   - 上架门禁：逐渠道、逐地域校验，且下单时会**再次实时校验**——即使 listing 未及时下架，
     授权撤回/到期后也无法成交。
4. **共享库存 + 渠道预留（ATP）**：直播、馆内商店、快闪共享同一实物批次与可承诺库存
   （在手 + 在途未废数量 − 已占用）；每个渠道有独立预留额度。下单在单个
   `BEGIN IMMEDIATE` 事务内同时检查全局量与渠道预留，杜绝超卖与渠道间重复占用。
5. **事件驱动处置且保留合同承诺**：
   - 授权撤回：立即停售相关 listing、停产未完成采购单（`committed=1` 的产能承诺保留，
     按合同与供应商结算，不删单），未发货订单进入"退款或替代"待决队列（可自动退款）；
     已发货部分不追回。
   - 授权到期：`license-expiry-sweep` 到期巡查自动停售停产；授权期内已成交订单继续履行，
     按下单时快照的许可结算版税。
   - 特展改期：比对新展期与许可期限，生成续签与预防性停售待办。
   - 供应商延期：采购单延期并保留承诺；新交期晚于许可到期时给出更换供应商/调减/续签建议。
   - 质检不合格：不合格数量隔离、不得入库；若因此 ATP 为负，按订单生成缺口的退款/替代动作。
6. **版税按有效授权与实际净销售结算**：发货确认销售，未发货退款不冲减；
   支持比例分成与单件费用两种条款，结算保留逐笔销售/退款明细。
7. **风险看板与双向追溯**：`/dashboard` 汇总待续权利、库存敞口（ATP）、延期/停产采购单、
   停售 listing、待决处置；`/versions/{code}/trace` 从任一商品追溯作品来源、权利人、
   许可与证据、内容审核、采购批次质检、库存台账、上架、订单/发货/退款、版税与相关事件。

## 使用方式

```bash
PYTHONPATH=src python -m museum_merch.main     # 启动服务，默认 0.0.0.0:8080
PYTHONPATH=src python -m unittest discover -s tests   # 执行测试（41 个用例）
```

健康检查：`GET /health`。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/holders` `/exhibitions` `/artworks` | 权利人、特展、作品建档 |
| POST | `/licenses` | 登记许可（用途/渠道/地域/期限/版税） |
| POST | `/licenses/{code}/documents` | 上传授权证据（合同/邮件/同意书/续签/撤回通知） |
| POST | `/licenses/{code}/revoke` `/renew` | 撤回（触发停售停产退款流程）/ 续签 |
| POST | `/incidents/license-expiry-sweep` | 授权到期巡查（可带 `date` 模拟日期） |
| POST | `/versions` | 创建产品版本（`is_derivative` 标记改编） |
| POST | `/versions/{code}/review` | 内容审核结论 approved/rejected/changes_requested |
| GET | `/versions/{code}/gate?channel=&region=&date=` | 采购/上架门禁的可解释结论 |
| GET | `/artworks/{code}/promotion-check` | 宣传用途独立审查 |
| POST | `/suppliers` `/purchase-orders` `/purchase-orders/{code}/lock` | 供应商与带门禁的产能锁定 |
| POST | `/batches` `/batches/{code}/qc` `/batches/{code}/receive` | 开批、质检（不合格自动立事件）、合格入库 |
| POST | `/reservations` | 渠道预留额度 |
| POST | `/listings` | 逐渠道/地域上架（过门） |
| POST | `/orders` `/orders/{code}/ship` `/orders/{code}/cancel` | 下单（防超卖）、部分发货、取消释放 |
| POST | `/items/{id}/refund` `/items/{id}/substitute` | 退款 / 以权利干净版本替代 |
| GET | `/inventory/{code}` | 在手/在途/占用/ATP 与各渠道预留占用 |
| POST | `/royalty/settle` | 按周期与有效许可结算版税 |
| POST | `/incidents/exhibition-rescheduled` `/incidents/supplier-delay` | 改期 / 延期事件 |
| GET | `/incident-actions/pending` | 待人工决策的处置队列 |
| POST | `/incident-actions/{id}/resolve` | 决策 refund/substitute/renew/decline |
| GET | `/dashboard?exhibition=` | 临展风险看板 |
| GET | `/versions/{code}/trace` | 商品全链路追溯 |

业务规则冲突返回 `422`，响应体 `reasons` 给出逐维度的可解释原因。

## 资料目录

- `docs/rights.md`：作品权利与履约边界说明。
- `src/museum_merch/rights.py`：许可覆盖判定与门禁引擎。
- `src/museum_merch/catalog.py` `production.py` `sales.py` `incidents.py` `reporting.py`：
  建档授权、采购生产、库存订单版税、事件处置、看板追溯。
- `fixtures/license-scope.json`：授权范围示例。
