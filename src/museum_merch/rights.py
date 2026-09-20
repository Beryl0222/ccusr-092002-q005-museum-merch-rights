"""权利审查引擎。

许可边界由六个维度构成：用途(use)、地域(region)、渠道(channel)、期限、证据、状态。
宣传(exhibition-promotion)、商品复制(product-reproduction)、改编(product-adaptation)
是互相独立的用途，任何一个维度缺失都必须给出可解释的原因，而不是含糊地拒绝。
"""

import json
from datetime import date

USE_PROMOTION = "exhibition-promotion"
USE_REPRODUCTION = "product-reproduction"
USE_ADAPTATION = "product-adaptation"

REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"


def today() -> str:
    return date.today().isoformat()


def _load_list(raw: str) -> list[str]:
    return json.loads(raw or "[]")


def _match(code: str, allowed: list[str]) -> bool:
    return "*" in allowed or code in allowed


def license_covers(
    lic: dict,
    use: str,
    channel: str | None = None,
    region: str | None = None,
    on_date: str | None = None,
) -> bool:
    """判断一条许可在给定维度上是否覆盖；证据在 readiness 层另行校验。"""
    on_date = on_date or today()
    if lic["status"] != "active":
        return False
    if not (lic["valid_from"] <= on_date <= lic["valid_until"]):
        return False
    if use not in _load_list(lic["uses_json"]):
        return False
    if channel is not None and not _match(channel, _load_list(lic["channels_json"])):
        return False
    if region is not None and not _match(region, _load_list(lic["regions_json"])):
        return False
    return True


def required_uses(version: dict) -> list[str]:
    uses = [USE_REPRODUCTION]
    if version["is_derivative"]:
        uses.append(USE_ADAPTATION)
    return uses


def _license_doc_count(db, license_id: int) -> int:
    return db.execute(
        "SELECT COUNT(*) AS n FROM license_documents WHERE license_id = ?",
        (license_id,),
    ).fetchone()["n"]


def effective_licenses(
    db,
    version: dict,
    channel: str | None = None,
    region: str | None = None,
    on_date: str | None = None,
    use: str | None = None,
) -> list[dict]:
    """返回当前有效、且有证据支撑的候选许可（仅匹配状态/期限/维度，不做去用途分组）。"""
    rows = db.execute(
        "SELECT * FROM licenses WHERE artwork_id = ? AND status = 'active'",
        (version["artwork_id"],),
    ).fetchall()
    result = []
    for row in rows:
        lic = dict(row)
        if not license_covers(lic, use or USE_REPRODUCTION, channel, region, on_date):
            continue
        if _license_doc_count(db, lic["id"]) == 0:
            continue  # 无证据的许可视为未确认，不产生效力
        result.append(lic)
    return result


def _explain_license_gap(
    lic: dict, use: str, channel: str | None, region: str | None, on_date: str
) -> list[str]:
    reasons: list[str] = []
    if lic["status"] == "revoked":
        reasons.append(f"许可 {lic['code']} 已于 {lic['revoked_at']} 撤回")
    elif lic["status"] == "expired":
        reasons.append(f"许可 {lic['code']} 已标记到期")
    elif on_date < lic["valid_from"]:
        reasons.append(f"许可 {lic['code']} 自 {lic['valid_from']} 才生效")
    elif on_date > lic["valid_until"]:
        reasons.append(f"许可 {lic['code']} 已于 {lic['valid_until']} 到期")
    if use not in _load_list(lic["uses_json"]):
        reasons.append(f"许可 {lic['code']} 未授予用途 {use}")
    if channel is not None and not _match(channel, _load_list(lic["channels_json"])):
        reasons.append(f"许可 {lic['code']} 未覆盖渠道 {channel}")
    if region is not None and not _match(region, _load_list(lic["regions_json"])):
        reasons.append(f"许可 {lic['code']} 未覆盖地域 {region}")
    return reasons


def evaluate_version(
    db,
    version: dict,
    channel: str | None = None,
    region: str | None = None,
    on_date: str | None = None,
    require_review: bool = True,
) -> dict:
    """对产品版本做权利 + 审核门禁评估，返回每个维度的可解释结论。

    - 采购门禁：channel/region 为空，只要求用途层面存在有效且有证据的许可，且内容审核通过。
    - 上架门禁：给定 channel/region，要求许可在该渠道与地域同样覆盖。
    - 宣传物料走 evaluate_promotion，用途不同，不可与商品许可混用。
    """
    on_date = on_date or today()
    reasons: list[str] = []
    licenses = db.execute(
        "SELECT * FROM licenses WHERE artwork_id = ?", (version["artwork_id"],)
    ).fetchall()
    licenses = [dict(r) for r in licenses]

    uses_ok: dict[str, dict] = {}
    for use in required_uses(version):
        covering = [
            lic
            for lic in licenses
            if license_covers(lic, use, channel, region, on_date)
            and _license_doc_count(db, lic["id"]) > 0
        ]
        if covering:
            uses_ok[use] = {"ok": True, "licenses": [l["code"] for l in covering]}
        else:
            detail: list[str] = []
            # 候选只按用途筛选；渠道/地域/期限/证据的具体缺口由逐条解释给出
            candidates = [lic for lic in licenses
                          if use in _load_list(lic["uses_json"])]
            if not candidates:
                detail.append(f"不存在覆盖用途 {use} 的许可记录")
            for lic in candidates:
                if _license_doc_count(db, lic["id"]) == 0:
                    detail.append(f"许可 {lic['code']} 缺少授权证据文件")
                detail.extend(
                    _explain_license_gap(lic, use, channel, region, on_date)
                )
            uses_ok[use] = {"ok": False, "reasons": sorted(set(detail))}
            reasons.extend(detail)

    review = db.execute(
        "SELECT * FROM content_reviews WHERE version_id = ? ORDER BY decided_at DESC, id DESC LIMIT 1",
        (version["id"],),
    ).fetchone()
    review_ok = bool(review and review["decision"] == REVIEW_APPROVED)
    if require_review and not review_ok:
        if review is None:
            reasons.append("内容审核尚未进行")
        elif review["decision"] == REVIEW_REJECTED:
            reasons.append(f"内容审核未通过（{review['notes'] or '见审核记录'}）")
        else:
            reasons.append("内容审核尚在修改流程中，未最终通过")

    return {
        "version": version["code"],
        "on_date": on_date,
        "channel": channel,
        "region": region,
        "rights_ok": all(v["ok"] for v in uses_ok.values()),
        "review_ok": review_ok,
        "ok": all(v["ok"] for v in uses_ok.values()) and (review_ok or not require_review),
        "uses": uses_ok,
        "reasons": reasons,
    }


def evaluate_promotion(
    db,
    artwork_id: int,
    channel: str | None,
    region: str | None,
    on_date: str | None = None,
) -> dict:
    """宣传图/宣传物料单独审查 exhibition-promotion 用途。"""
    on_date = on_date or today()
    pseudo = {"id": None, "code": "promotion", "artwork_id": artwork_id, "is_derivative": 0}
    licenses = db.execute(
        "SELECT * FROM licenses WHERE artwork_id = ?", (artwork_id,)
    ).fetchall()
    licenses = [dict(r) for r in licenses]
    covering = [
        lic
        for lic in licenses
        if license_covers(lic, USE_PROMOTION, channel, region, on_date)
        and _license_doc_count(db, lic["id"]) > 0
    ]
    result = {
        "use": USE_PROMOTION,
        "channel": channel,
        "region": region,
        "on_date": on_date,
        "ok": bool(covering),
        "licenses": [l["code"] for l in covering],
        "reasons": [],
    }
    if not covering:
        detail = [
            f"不存在覆盖宣传用途（渠道={channel or '*'}，地域={region or '*'}）的有效许可"
        ]
        for lic in licenses:
            if USE_PROMOTION in _load_list(lic["uses_json"]):
                if _license_doc_count(db, lic["id"]) == 0:
                    detail.append(f"许可 {lic['code']} 缺少授权证据文件")
                detail.extend(
                    _explain_license_gap(lic, USE_PROMOTION, channel, region, on_date)
                )
        result["reasons"] = sorted(set(detail))
    return result


def pick_order_license(
    db,
    version: dict,
    channel: str,
    region: str,
    on_date: str | None = None,
) -> dict | None:
    """下单时选定版税结算所依据的有效许可（覆盖商品复制用途），作为快照。"""
    candidates = effective_licenses(
        db, version, channel, region, on_date, USE_REPRODUCTION
    )
    if not candidates:
        return None
    pool = [l for l in candidates if l["royalty_rate"] or l["royalty_unit_fee"]]
    pool = pool or candidates
    # 同为有效许可时选期限最晚到期的，避免把版税快照到更早失效的一条
    pool.sort(key=lambda l: l["valid_until"], reverse=True)
    return pool[0]
