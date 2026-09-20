# 权利与履约边界

作品入藏不代表美术馆同时取得复制、改编和商业销售所需的全部权利。本系统把「许可边界」
落实为每条授权上三个互相独立、必须同时命中的维度，并要求凭证可核验：

| 维度 | 取值 | 说明 |
|---|---|---|
| 用途 use | `reproduction` 复制 / `adaptation` 改编 / `promotion` 宣传 | 宣传图获准不等于商品复制获准 |
| 渠道 channel | `museum-store` 馆内商店 / `official-online-store` 官方线上 / `livestream` 直播 / `pop-up` 外部快闪 | 渠道组合逐份授权约定 |
| 地域 region | ISO 国家/地区码（`CN`、`US`…） | 超出地域的销售一律不许可 |
| 期限 | `valid_from` ~ `valid_until`（含端点） | 到期自动失效，不靠人工记忆 |

- **凭证（evidence）**：合同、邮件、书面同意书登记在授权下，状态须为 `verified`；
  无已核验凭证的授权不通过任何闸门。
- **多权利主体**：一件作品可由家属、基金会、合作机构共有，版税按 `artwork_holder_split`
  的基点份额（合计 10000）拆分。
- **续展链**：续展/重签以 `supersedes_license_id` 关联旧授权，旧记录永不删除。

## 两道闸门

1. **采购闸门（procurement）**：版本计划进入的「全部渠道×地域」都要有有效授权、
   内容审核通过，才允许向供应商下单锁产能。结果写入 `gate_review` 快照。
2. **上架/销售闸门（listing / sale）**：逐渠道、逐地域匹配；下单与发货时再次实时
   校验，授权在生产期间失效则商品无法出库。

闸门明细记录每份候选授权的缺口（`use` / `channel` / `region` / `period` /
`withdrawn` / `no-evidence` / `evidence-unverified`），可向权利人解释「为什么不能卖」。

## 异常处置原则

产品从设计到销售经历内容审核、打样、采购、生产、检验、上架。许可撤回或到期时，
系统按 `事件(domain_event) → 工单(case_record) → 动作(case_action)` 处理：

- **确定性动作自动执行**：停售（`delist`）、未发货订单退款取消（`refund-unshipped`）、
  未确认采购单取消（`cancel-po`）、已确认采购单停产但保留合同（`halt-production`）、
  剩余库存隔离（`quarantine-stock`）。
- **需商业判断的动作挂起**：续展（`renew-license`）、替代版本（`substitute`）、
  重新下单（`reorder`）。有替代候选时未发货订单先挂起；确认替代则转入新版本，
  拒绝替代才退款。
- **已发生的承诺不删除**：已确认 PO、已发货销售、已计提版税全部保留，只是停产、
  停售或冲回。历史产品不能被简单删除。

事件类型：`license.withdrawn`、`license.expired`（由到期巡检 sweep 产生）、
`exhibition.rescheduled`、`supplier.delay`、`qc.failed`。

## 库存与渠道预留

直播、馆内商店、快闪共用一个实物批次池（`stock_ledger` 累计三个口径：
合格在库 on_hand / 预留 reserved / 隔离 quarantine）。每个渠道可设预留额度：

```
渠道 C 还可预留 = min(quota_C − used_C,            # 有额度渠道不超过自身额度
                      (on_hand − Σused) − Σ其他渠道额度缺口)
```

无额度渠道（如直播）只能争抢未被任何额度覆盖的共享部分；「直播秒空」无法吃掉
馆内/快闪额度。所有下单在单条 `BEGIN IMMEDIATE` 事务内读余量并写预留，
取消与部分发货逐件释放，杜绝超卖与重复占用。

## 版税

权责发生制：**发货**时按发货时点「当时有效」的授权约定计提（费率‰或按件），
多权利人按份额拆分；退款按件均比例冲回（负分录）；结算只汇总未结算分录。
版税基数是实际净销售（发货价 × 数量），未发货不产生版税。
