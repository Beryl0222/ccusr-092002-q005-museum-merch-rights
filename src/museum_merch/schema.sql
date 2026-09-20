-- 美术馆文创授权履约系统：贯穿策展、授权、打样、生产、销售的后端数据结构

-- 策展：特展可能改期，日期变更另由 domain_event 留痕
CREATE TABLE IF NOT EXISTS exhibition (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'scheduled', -- scheduled / ongoing / closed
    created_at  TEXT NOT NULL
);

-- 权利主体：艺术家本人、家属、艺术基金会、合作机构等
CREATE TABLE IF NOT EXISTS right_holder (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    kind    TEXT NOT NULL,                          -- artist / family / estate / institution
    contact TEXT
);

CREATE TABLE IF NOT EXISTS artwork (
    id            TEXT PRIMARY KEY,
    exhibition_id TEXT REFERENCES exhibition(id),
    title         TEXT NOT NULL,
    catalog_no    TEXT,
    created_at    TEXT NOT NULL
);

-- 一件作品可能由多个继承人/机构共有，版税按份额分配；无记录时视为单一授权方 100%
CREATE TABLE IF NOT EXISTS artwork_holder_split (
    artwork_id TEXT NOT NULL REFERENCES artwork(id),
    holder_id  TEXT NOT NULL REFERENCES right_holder(id),
    share_bps  INTEGER NOT NULL CHECK (share_bps > 0 AND share_bps <= 10000),
    PRIMARY KEY (artwork_id, holder_id)
);

-- 授权合同：续展/重签以 supersedes 关联形成版本链；撤回与到期只改状态，不删除记录
CREATE TABLE IF NOT EXISTS license (
    id                     TEXT PRIMARY KEY,
    code                   TEXT UNIQUE NOT NULL,
    artwork_id             TEXT NOT NULL REFERENCES artwork(id),
    holder_id              TEXT NOT NULL REFERENCES right_holder(id),
    sublicensable          INTEGER NOT NULL DEFAULT 0,
    status                 TEXT NOT NULL DEFAULT 'active', -- active / withdrawn / expired
    valid_from             TEXT NOT NULL,
    valid_until            TEXT NOT NULL,
    royalty_rate_bps       INTEGER,                    -- 按净销售额百分比（基点），与 per_unit 二选一
    royalty_per_unit_cents INTEGER,                    -- 按件计费
    supersedes_license_id  TEXT REFERENCES license(id),
    signed_at              TEXT,
    withdrawn_at           TEXT,
    withdraw_reason        TEXT,
    created_at             TEXT NOT NULL
);

-- 许可边界三个正交维度：用途 / 渠道 / 地域。宣传(promotion)与复制(reproduction)互不可替代
CREATE TABLE IF NOT EXISTS license_use (
    license_id TEXT NOT NULL REFERENCES license(id),
    use_type   TEXT NOT NULL, -- reproduction / adaptation / promotion
    PRIMARY KEY (license_id, use_type)
);
CREATE TABLE IF NOT EXISTS license_channel (
    license_id TEXT NOT NULL REFERENCES license(id),
    channel    TEXT NOT NULL, -- museum-store / official-online-store / livestream / pop-up
    PRIMARY KEY (license_id, channel)
);
CREATE TABLE IF NOT EXISTS license_region (
    license_id TEXT NOT NULL REFERENCES license(id),
    region     TEXT NOT NULL,
    PRIMARY KEY (license_id, region)
);

-- 授权凭证（合同、邮件、书面同意书），无已核验凭证的授权不得通过闸门
CREATE TABLE IF NOT EXISTS license_evidence (
    id          TEXT PRIMARY KEY,
    license_id  TEXT NOT NULL REFERENCES license(id),
    kind        TEXT NOT NULL, -- contract / email / consent-form
    document_ref TEXT NOT NULL,
    checksum    TEXT,
    status      TEXT NOT NULL DEFAULT 'verified', -- received / verified
    summary     TEXT,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product (
    id            TEXT PRIMARY KEY,
    sku           TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    exhibition_id TEXT REFERENCES exhibition(id),
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL
);

-- 产品版本：同一商品可因改版/换图产生多个版本，审批与权利审查都挂在版本上
CREATE TABLE IF NOT EXISTS product_version (
    id             TEXT PRIMARY KEY,
    product_id     TEXT NOT NULL REFERENCES product(id),
    version_no     INTEGER NOT NULL DEFAULT 1,
    artwork_id     TEXT NOT NULL REFERENCES artwork(id),
    use_type       TEXT NOT NULL,                  -- reproduction / adaptation
    title          TEXT NOT NULL,
    spec           TEXT,
    price_cents    INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'draft',  -- draft / approved / blocked
    content_status TEXT NOT NULL DEFAULT 'pending',-- pending / approved / rejected
    blocked_reason TEXT,
    created_at     TEXT NOT NULL
);

-- 版本计划进入的市场（渠道 × 地域），是权利闸门逐条核对的需求清单
CREATE TABLE IF NOT EXISTS version_market (
    version_id TEXT NOT NULL REFERENCES product_version(id),
    channel    TEXT NOT NULL,
    region     TEXT NOT NULL,
    PRIMARY KEY (version_id, channel, region)
);

CREATE TABLE IF NOT EXISTS content_review (
    id         TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES product_version(id),
    decision   TEXT NOT NULL,                       -- approved / rejected
    reviewer   TEXT,
    notes      TEXT,
    decided_at TEXT NOT NULL
);

-- 闸门审查快照：采购前与每次上架各存一份，落库当时匹配到的授权、缺失项与证据
CREATE TABLE IF NOT EXISTS gate_review (
    id         TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES product_version(id),
    stage      TEXT NOT NULL,                       -- procurement / listing / sale
    channel    TEXT,
    region     TEXT,
    decision   TEXT NOT NULL,                       -- pass / fail
    detail     TEXT NOT NULL,                      -- JSON
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listing (
    id             TEXT PRIMARY KEY,
    version_id     TEXT NOT NULL REFERENCES product_version(id),
    channel        TEXT NOT NULL,
    region         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active', -- active / stopped
    gate_review_id TEXT REFERENCES gate_review(id),
    listed_at      TEXT NOT NULL,
    stopped_at     TEXT,
    case_id        TEXT
);

CREATE TABLE IF NOT EXISTS supplier (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    lead_time_days INTEGER
);

-- 采购单：确认(confirmed)即构成对供应商的合同承诺，撤回授权也不删除，只允许停产/保留
CREATE TABLE IF NOT EXISTS purchase_order (
    id              TEXT PRIMARY KEY,
    po_no           TEXT UNIQUE NOT NULL,
    version_id      TEXT NOT NULL REFERENCES product_version(id),
    supplier_id     TEXT NOT NULL REFERENCES supplier(id),
    qty_ordered     INTEGER NOT NULL,
    unit_cost_cents INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft',
    -- draft / confirmed / in-production / halted / delayed / completed / cancelled
    committed       INTEGER NOT NULL DEFAULT 0,
    expected_at     TEXT,
    created_at      TEXT NOT NULL,
    confirmed_at    TEXT
);

CREATE TABLE IF NOT EXISTS production_batch (
    id           TEXT PRIMARY KEY,
    batch_no     TEXT UNIQUE NOT NULL,
    po_id        TEXT NOT NULL REFERENCES purchase_order(id),
    version_id   TEXT NOT NULL REFERENCES product_version(id),
    qty_produced INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'qc-pending', -- qc-pending / qualified / partial / rejected
    created_at   TEXT NOT NULL,
    qualified_at TEXT
);

CREATE TABLE IF NOT EXISTS quality_check (
    id            TEXT PRIMARY KEY,
    batch_id      TEXT NOT NULL REFERENCES production_batch(id),
    qty_inspected INTEGER NOT NULL,
    qty_passed    INTEGER NOT NULL DEFAULT 0,
    qty_failed    INTEGER NOT NULL DEFAULT 0,
    decision      TEXT NOT NULL,                     -- pass / partial / fail
    notes         TEXT,
    checked_at    TEXT NOT NULL
);

-- 共享库存台账：三渠道共用一个实物池，预留(reserved)与合格在库(on_hand)分别累计
CREATE TABLE IF NOT EXISTS stock_ledger (
    id               TEXT PRIMARY KEY,
    version_id       TEXT NOT NULL REFERENCES product_version(id),
    batch_id         TEXT REFERENCES production_batch(id),
    channel          TEXT,
    delta_on_hand    INTEGER NOT NULL DEFAULT 0,
    delta_reserved   INTEGER NOT NULL DEFAULT 0,
    delta_quarantine INTEGER NOT NULL DEFAULT 0,
    reason           TEXT NOT NULL,
    ref              TEXT,
    created_at       TEXT NOT NULL
);

-- 各渠道预留额度（未被任何额度覆盖的部分为共享可争抢库存）
CREATE TABLE IF NOT EXISTS channel_quota (
    version_id TEXT NOT NULL REFERENCES product_version(id),
    channel    TEXT NOT NULL,
    quota_qty  INTEGER NOT NULL,
    PRIMARY KEY (version_id, channel)
);

CREATE TABLE IF NOT EXISTS sales_order (
    id           TEXT PRIMARY KEY,
    order_no     TEXT UNIQUE NOT NULL,
    channel      TEXT NOT NULL,
    region       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',
    -- active / partially-shipped / shipped / cancelled / partially-cancelled
    customer_ref TEXT,
    total_cents  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_item (
    id             TEXT PRIMARY KEY,
    order_id       TEXT NOT NULL REFERENCES sales_order(id),
    version_id     TEXT NOT NULL REFERENCES product_version(id),
    qty            INTEGER NOT NULL,
    qty_shipped    INTEGER NOT NULL DEFAULT 0,
    qty_cancelled  INTEGER NOT NULL DEFAULT 0,
    unit_price_cents INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open'      -- open / shipped / cancelled / substituted
);

CREATE TABLE IF NOT EXISTS stock_reservation (
    id           TEXT PRIMARY KEY,
    item_id      TEXT NOT NULL REFERENCES order_item(id),
    version_id   TEXT NOT NULL REFERENCES product_version(id),
    channel      TEXT NOT NULL,
    qty_held     INTEGER NOT NULL,
    qty_consumed INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'held',       -- held / consumed / released
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shipment (
    id         TEXT PRIMARY KEY,
    order_id   TEXT NOT NULL REFERENCES sales_order(id),
    item_id    TEXT NOT NULL REFERENCES order_item(id),
    version_id TEXT NOT NULL REFERENCES product_version(id),
    batch_id   TEXT NOT NULL REFERENCES production_batch(id),
    qty        INTEGER NOT NULL,
    shipped_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refund (
    id         TEXT PRIMARY KEY,
    order_id   TEXT NOT NULL REFERENCES sales_order(id),
    item_id    TEXT REFERENCES order_item(id),
    version_id TEXT NOT NULL REFERENCES product_version(id),
    qty        INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    case_id    TEXT,
    created_at TEXT NOT NULL
);

-- 事件与处置工单：所有异常流程都可解释、可审计
CREATE TABLE IF NOT EXISTS domain_event (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    aggregate_ref TEXT,
    payload     TEXT NOT NULL DEFAULT '{}',
    processed   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_record (
    id          TEXT PRIMARY KEY,
    case_no     TEXT UNIQUE NOT NULL,
    event_id    TEXT REFERENCES domain_event(id),
    version_id  TEXT REFERENCES product_version(id),
    kind        TEXT NOT NULL,
    -- rights-withdrawal / rights-expired / reschedule / supplier-delay / qc-failed
    title       TEXT NOT NULL,
    explanation TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT '{}',          -- JSON: 受影响版本/订单/采购单
    replacement_version_id TEXT REFERENCES product_version(id),
    status      TEXT NOT NULL DEFAULT 'open',        -- open / actioned / closed
    created_at  TEXT NOT NULL,
    actioned_at TEXT
);

CREATE TABLE IF NOT EXISTS case_action (
    id          TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL REFERENCES case_record(id),
    action_type TEXT NOT NULL,
    -- delist / halt-production / cancel-po / keep-commitment / quarantine-stock /
    -- refund-unshipped / substitute / renew-license / reorder / unquarantine / await
    target_ref  TEXT,
    detail      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'proposed',    -- proposed / executed / skipped
    result      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    executed_at TEXT
);

-- 版税权责发生制：发货按当时有效授权计提，退款按原费率冲回；结算只汇总未结算分录
CREATE TABLE IF NOT EXISTS royalty_accrual (
    id         TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES license(id),
    holder_id  TEXT NOT NULL REFERENCES right_holder(id),
    artwork_id TEXT NOT NULL REFERENCES artwork(id),
    version_id TEXT NOT NULL REFERENCES product_version(id),
    basis      TEXT NOT NULL,                        -- shipment / refund
    ref_id     TEXT NOT NULL,                       -- shipment 或 refund 的 id
    qty        INTEGER NOT NULL,
    net_cents  INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    rate_bps   INTEGER,
    share_bps  INTEGER NOT NULL,
    period_key TEXT NOT NULL,                        -- YYYY-MM
    settlement_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS royalty_settlement (
    id           TEXT PRIMARY KEY,
    holder_id    TEXT NOT NULL REFERENCES right_holder(id),
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft',      -- draft / confirmed / paid
    created_at   TEXT NOT NULL,
    paid_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_license_artwork ON license(artwork_id);
CREATE INDEX IF NOT EXISTS idx_version_product ON product_version(product_id);
CREATE INDEX IF NOT EXISTS idx_ledger_version ON stock_ledger(version_id);
CREATE INDEX IF NOT EXISTS idx_item_order ON order_item(order_id);
CREATE INDEX IF NOT EXISTS idx_shipment_version ON shipment(version_id);
CREATE INDEX IF NOT EXISTS idx_accrual_holder ON royalty_accrual(holder_id, period_key);
