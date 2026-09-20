"""SQLite 数据连接与表结构。

表结构贯穿：策展(展览/作品) → 授权(许可/证据) → 产品版本与内容审核 →
采购/生产/质检/入库 → 渠道预留与共享库存 → 订单/发货/退款 → 版税结算，
并以 incidents / incident_actions 记录每次风险事件触发的可解释处置。
"""

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS exhibitions (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled', -- scheduled / ongoing / rescheduled / closed
    note TEXT
);

CREATE TABLE IF NOT EXISTS right_holders (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                       -- artist / family / estate / institution
    contact TEXT
);

CREATE TABLE IF NOT EXISTS artworks (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    holder_id INTEGER NOT NULL REFERENCES right_holders(id),
    exhibition_id INTEGER REFERENCES exhibitions(id)
);

CREATE TABLE IF NOT EXISTS licenses (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    artwork_id INTEGER NOT NULL REFERENCES artworks(id),
    holder_id INTEGER NOT NULL REFERENCES right_holders(id),
    uses_json TEXT NOT NULL,        -- ["exhibition-promotion","product-reproduction","product-adaptation"]
    channels_json TEXT NOT NULL,    -- 渠道码，["*"] 表示不限
    regions_json TEXT NOT NULL,     -- 地域码，["*"] 表示不限
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    sublicensable INTEGER NOT NULL DEFAULT 0,
    royalty_rate TEXT,              -- 净销售额分成比例，如 "0.12"
    royalty_unit_fee TEXT,          -- 或每件固定费用
    status TEXT NOT NULL DEFAULT 'active',  -- active / revoked / expired
    revoked_at TEXT,
    signed_at TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS license_documents (
    id INTEGER PRIMARY KEY,
    license_id INTEGER NOT NULL REFERENCES licenses(id),
    doc_type TEXT NOT NULL,         -- contract / email / written-consent / renewal / revocation-notice
    doc_ref TEXT NOT NULL,
    checksum TEXT,
    issued_by TEXT,
    received_at TEXT NOT NULL
);

-- 产品版本：同一产品的不同授权/设计版本分别建档，便于逐版本门禁与追溯
CREATE TABLE IF NOT EXISTS product_versions (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    artwork_id INTEGER NOT NULL REFERENCES artworks(id),
    is_derivative INTEGER NOT NULL DEFAULT 0,  -- 改编作品需 product-adaptation
    status TEXT NOT NULL DEFAULT 'draft',
    -- draft / in_review / approved / rejected / production / on_sale /
    -- sale_suspended / production_halted / closed
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS content_reviews (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES product_versions(id),
    decision TEXT NOT NULL,         -- approved / rejected / changes_requested
    reviewer TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    notes TEXT,
    doc_ref TEXT
);

-- 逐渠道/地域的上架记录：授权撤回时可分别停售
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES product_versions(id),
    channel TEXT NOT NULL,
    region TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', -- active / suspended / delisted
    listed_at TEXT NOT NULL,
    suspended_at TEXT,
    reason TEXT,
    UNIQUE (version_id, channel, region)
);

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    lead_time_days INTEGER NOT NULL DEFAULT 30
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    version_id INTEGER NOT NULL REFERENCES product_versions(id),
    qty INTEGER NOT NULL,
    unit_cost TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    status TEXT NOT NULL DEFAULT 'draft',
    -- draft / locked / in_production / delayed / production_halted / completed / closed
    committed INTEGER NOT NULL DEFAULT 0, -- 锁定产能即构成对供应商的合同承诺，撤回也不删除
    locked_at TEXT,
    expected_at TEXT,
    completed_at TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS production_batches (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    version_id INTEGER NOT NULL REFERENCES product_versions(id),
    qty_planned INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'sampling',
    -- sampling / in_production / qc_passed / qc_failed / quarantined / received
    qc_passed_qty INTEGER NOT NULL DEFAULT 0,
    qc_failed_qty INTEGER NOT NULL DEFAULT 0,
    received_qty INTEGER NOT NULL DEFAULT 0,
    shipped_qty INTEGER NOT NULL DEFAULT 0,
    inspected_at TEXT,
    received_at TEXT,
    inspector TEXT,
    note TEXT
);

-- 版本级共享库存汇总；stock_ledger 保留逐笔出入库证据
CREATE TABLE IF NOT EXISTS inventory (
    version_id INTEGER PRIMARY KEY REFERENCES product_versions(id),
    on_hand INTEGER NOT NULL DEFAULT 0,
    allocated INTEGER NOT NULL DEFAULT 0,   -- 已被订单硬占用、尚未出库
    shipped INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stock_ledger (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES product_versions(id),
    batch_id INTEGER REFERENCES production_batches(id),
    change_qty INTEGER NOT NULL,
    reason TEXT NOT NULL,
    channel TEXT,
    ref TEXT,
    created_at TEXT NOT NULL
);

-- 各渠道在共享库存中的预留额度（如直播 60 / 馆内 40）
CREATE TABLE IF NOT EXISTS channel_reservations (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES product_versions(id),
    channel TEXT NOT NULL,
    reserved_qty INTEGER NOT NULL,
    UNIQUE (version_id, channel)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    channel TEXT NOT NULL,
    region TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', -- open / partially_shipped / shipped / cancelled
    customer_ref TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    version_id INTEGER NOT NULL REFERENCES product_versions(id),
    qty INTEGER NOT NULL,
    allocated_qty INTEGER NOT NULL DEFAULT 0,
    shipped_qty INTEGER NOT NULL DEFAULT 0,
    refunded_qty INTEGER NOT NULL DEFAULT 0,
    unit_price TEXT NOT NULL,           -- 单件净价
    license_id INTEGER REFERENCES licenses(id), -- 下单时有效的授权快照
    substitute_for_item_id INTEGER REFERENCES order_items(id),
    status TEXT NOT NULL DEFAULT 'open' -- open / partial / shipped / cancelled / refunded
);

CREATE TABLE IF NOT EXISTS shipments (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    item_id INTEGER NOT NULL REFERENCES order_items(id),
    batch_id INTEGER REFERENCES production_batches(id),
    qty INTEGER NOT NULL,
    shipped_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    item_id INTEGER REFERENCES order_items(id),
    qty INTEGER NOT NULL,
    amount TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS royalty_settlements (
    id INTEGER PRIMARY KEY,
    license_id INTEGER NOT NULL REFERENCES licenses(id),
    period_from TEXT NOT NULL,
    period_to TEXT NOT NULL,
    net_sales TEXT NOT NULL,
    rate TEXT,
    amount TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'settled',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS royalty_settlement_lines (
    id INTEGER PRIMARY KEY,
    settlement_id INTEGER NOT NULL REFERENCES royalty_settlements(id),
    kind TEXT NOT NULL,                 -- sale / refund
    item_id INTEGER REFERENCES order_items(id),
    shipment_id INTEGER REFERENCES shipments(id),
    refund_id INTEGER REFERENCES refunds(id),
    qty INTEGER NOT NULL,
    amount TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

-- 风险事件：授权撤回 / 特展改期 / 供应商延期 / 质检不合格
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL,
    subject TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open', -- open / processed
    summary TEXT,
    occurred_at TEXT NOT NULL,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS incident_actions (
    id INTEGER PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    action_type TEXT NOT NULL,
    -- halt_production / suspend_sale / refund_or_substitute /
    -- renew_license / rework_or_reorder / preserve_commitment
    entity_type TEXT NOT NULL,
    entity_ref TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'applied', -- applied / pending / resolved / declined
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_ref TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_licenses_artwork ON licenses(artwork_id);
CREATE INDEX IF NOT EXISTS idx_versions_artwork ON product_versions(artwork_id);
CREATE INDEX IF NOT EXISTS idx_pos_version ON purchase_orders(version_id);
CREATE INDEX IF NOT EXISTS idx_batches_version ON production_batches(version_id);
CREATE INDEX IF NOT EXISTS idx_items_version ON order_items(version_id);
CREATE INDEX IF NOT EXISTS idx_listings_version ON listings(version_id);
CREATE INDEX IF NOT EXISTS idx_actions_incident ON incident_actions(incident_id);
"""


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(
        os.getenv("DATABASE_PATH", "museum-merch.db"),
        check_same_thread=False, timeout=30,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row
    return connection


def init_db(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.commit()
