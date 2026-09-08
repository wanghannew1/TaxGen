"""tax_adjust.py - 下月多退少补建议：对比系统预算个税与回盘已报税额，生成调整建议

本模块将 Oracle 系统预算个人所得税（TC8M status=2 聚合）与 SQLite 回盘
申报税款（filing_record 税款计算）进行逐人比对，计算差额并给出下月
少扣（退）或多扣（补）的建议，同时按结算单元汇总调整金额。
"""
from tax_return import get_system_aggregate, _get_unit_names
from filing_history import get_filing_map


def compare_tax(conn, month):
    """比对系统预算个税与回盘税款，返回下月多退少补建议。

    Args:
        conn: Oracle 数据库连接（只读，由 get_system_aggregate 使用）
        month: 月份，如 202606（int）

    Returns:
        dict: {
            "month": month,
            "total": {"detail_count", "refund_sum", "collect_sum", "net"},
            "units": [{"unit", "unit_name", "count", "refund", "collect", "net"}],
            "details": [{...per person...}],
        }
    """
    # --- 数据源 ---
    system, combos = get_system_aggregate(conn, month)
    returns = get_filing_map(month, "税款计算")

    # --- 单位名称 ---
    all_unit_keys = {k for s in system.values() for k in s.get("unit_keys", {(s.get("unit", 0), month, "1")})}
    unit_names = _get_unit_names(conn, list(all_unit_keys))

    details = []
    refund_sum = 0.0
    collect_sum = 0.0
    # units 聚合: unit -> {count, refund, collect}
    units_acc = {}

    # --- 系统侧人员 ---
    for cert, s in system.items():
        # unit_name / unit_periods — 与 compare_month 同逻辑
        keys = s.get("unit_keys", {(s.get("unit", 0), month, "1")})
        by_unit = {}
        for k in sorted(keys):
            by_unit.setdefault(k[0], []).append(
                (k[1], k[2], unit_names.get(k, "") or str(k[0]))
            )
        unit_names_list = []
        periods_list = []
        for u, items in by_unit.items():
            unit_names_list.append(items[0][2])
            periods_list.append(
                "、".join(f"{m}-批{s}" for m, s, _ in sorted(items, key=lambda x: (x[0], str(x[1]))))
            )
        uname = "、".join(unit_names_list)
        uperiods = " | ".join(periods_list)

        sys_tax = round(float(s["tax"]), 2)
        r = returns.get(cert)

        if r is None:
            # 回盘无此人员
            detail = {
                "cert_no": cert, "name": s["name"], "unit_name": uname,
                "unit_periods": uperiods,
                "sys_tax": sys_tax, "ret_tax": None, "diff": None,
                "status": "回盘无记录", "advice": "回盘无此人员，无法建议",
            }
        else:
            ret_tax = round(float(r["tax_due"]), 2)
            diff = round(sys_tax - ret_tax, 2)
            if abs(diff) < 0.01:
                status = "持平"
                advice = "无差异"
            elif diff > 0:
                status = "需退"
                advice = f"下月少扣{abs(diff):.2f}元（退）"
            else:
                status = "需补"
                advice = f"下月多扣{abs(diff):.2f}元（补）"
            detail = {
                "cert_no": cert, "name": s["name"], "unit_name": uname,
                "unit_periods": uperiods,
                "sys_tax": sys_tax, "ret_tax": ret_tax, "diff": diff,
                "status": status, "advice": advice,
            }
            # 资金汇总
            if diff > 0:
                refund_sum += diff
            elif diff < 0:
                collect_sum += abs(diff)
            # 单位聚合（仅 |diff|>=0.01 且有 sys_tax 的人员）
            if abs(diff) >= 0.01:
                # primary unit = sorted(unit_keys) 首个
                primary_unit = sorted(keys)[0][0]
                if primary_unit not in units_acc:
                    units_acc[primary_unit] = {"count": 0, "refund": 0.0, "collect": 0.0}
                units_acc[primary_unit]["count"] += 1
                if diff > 0:
                    units_acc[primary_unit]["refund"] += diff
                elif diff < 0:
                    units_acc[primary_unit]["collect"] += abs(diff)

        details.append(detail)

    # --- 回盘有但系统无的人员 ---
    for cert, r in returns.items():
        if cert not in system:
            ret_tax = round(float(r["tax_due"]), 2)
            details.append({
                "cert_no": cert, "name": r["name"], "unit_name": r.get("remark", ""),
                "unit_periods": "",
                "sys_tax": None, "ret_tax": ret_tax, "diff": None,
                "status": "系统无记录", "advice": "系统无此人员，无法建议",
            })
            # 系统无记录人员不计入 refund/collect

    # --- 构建 units 列表 ---
    # 获取单位名称（仅查询有调整的单位）
    unit_name_map = {}
    if units_acc:
        unit_keys_for_names = []
        # 为每个 unit 找到一个代表 combo 查询名称
        for u in units_acc:
            # 从 combos 中找到该 unit 的一个代表
            found = False
            for (unit, sm, seq), info in combos.items():
                if unit == u:
                    unit_keys_for_names.append((unit, sm, seq))
                    found = True
                    break
            if not found:
                unit_keys_for_names.append((u, month, "1"))
        unit_name_map = _get_unit_names(conn, unit_keys_for_names)

    units = []
    for u, acc in units_acc.items():
        # 用找到的代表名称
        uname = ""
        for (unit, sm, seq), name in unit_name_map.items():
            if unit == u:
                uname = name
                break
        net = round(acc["refund"] - acc["collect"], 2)
        units.append({
            "unit": u, "unit_name": uname, "count": acc["count"],
            "refund": round(acc["refund"], 2), "collect": round(acc["collect"], 2),
            "net": net,
        })
    # 按 |net| 降序
    units.sort(key=lambda x: abs(x["net"]), reverse=True)

    # --- total ---
    refund_sum = round(refund_sum, 2)
    collect_sum = round(collect_sum, 2)
    total = {
        "detail_count": len(details),
        "refund_sum": refund_sum,
        "collect_sum": collect_sum,
        "net": round(refund_sum - collect_sum, 2),
    }

    return {
        "month": month,
        "total": total,
        "units": units,
        "details": details,
    }
