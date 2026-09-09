"""tax_zero.py - 零申报（工资为0人员是否生成零申报）建议与过滤

用户需求: 程序自动判定(工资为0不增员不报税)难以满足实际场景, 由用户在生成前
通过结算单元粒度 + 员工粒度的零申报建议挑选: 哪些人生成零申报, 哪些不生成。

口径 (用户确认):
- 判定: 单条记录本期收入=0 (报税公式逐条计算, 与生成文件"本期收入"列对应)
- 范围: 包含已被"工资为0不增员不报税"配置排除的人 (可反转恢复生成);
        完全排除单元 (exclude_all) 仍强制排除, 不进入候选
- 建议: 配置单元(38973/41100等 zero_salary_no_add=1) → 默认 skip (不生成);
        其他单元 → 默认 declare (生成零申报)
- 持久化: config_db.zero_override, 用户"记住本次选择"后下次默认沿用
"""
from decimal import Decimal

from queries import get_salary_records
from config_db import get_zero_overrides, get_zero_salary_unit_codes, get_excluded_unit_codes
from templates_gen.formulas import calc_本期收入


def _income_of_dict(d: dict) -> Decimal:
    """tc93_all/abnormal 字典记录的本期收入 (公式与 calc_本期收入 一致)。"""
    return (Decimal(d.get("ATC93AA") or 0) - Decimal(d.get("ATC936") or 0)
            - Decimal(d.get("ATC93BD") or 0) - Decimal(d.get("ATC93BE") or 0)
            + Decimal(d.get("ATC93X3") or 0) - Decimal(d.get("ATC93E") or 0))


def _unit_of(rec) -> int:
    return int(rec.结算单元 or 0)


def _unit_of_dict(d: dict) -> int:
    return int(d.get("ATB930") or 0)


def _cert_of(rec) -> str:
    return str(rec.身份证 or rec.职工号 or "").strip()


def _cert_of_dict(d: dict) -> str:
    return str(d.get("身份证") or d.get("AAC001") or "").strip()


def build_zero_salary_suggestions(conn, pay_month, combos):
    """扫描指定发放月已确认组合中本期收入=0 的人员, 给出生成/不生成零申报建议。

    Args:
        conn: Oracle 连接 (只读)
        pay_month: 发放月份, 如 202606
        combos: 前端确认的组合列表, 每项含 unit/salary_month/seq 等

    Returns:
        dict: {
            "pay_month", "units": [
                {"unit", "unit_name", "config", "suggested", "count", "income_total",
                 "persons": [{"cert_no", "name", "emp_no", "salary_months",
                              "income_total", "suggested", "reason",
                              "default_chosen", "persisted"}]}
            ]
        }
    """
    combo_set = {(int(c.get("unit", 0) or 0), int(c.get("salary_month", 0) or 0),
                  str(c.get("seq", "") or "")) for c in combos}
    salary_months = sorted({c[1] for c in combo_set})
    if not combo_set or not salary_months:
        return {"pay_month": pay_month, "units": []}

    records = []
    for sm in salary_months:
        records.extend(get_salary_records(conn, sm))
    checked = [r for r in records
               if (_unit_of(r), r.工资所属年月, r.当月批次) in combo_set]

    zero_codes = set(get_zero_salary_unit_codes())
    excl_codes = set(get_excluded_unit_codes())
    overrides = get_zero_overrides()

    units = {}
    for rec in checked:
        if _unit_of(rec) in excl_codes:
            continue
        if calc_本期收入(rec) != 0:
            continue
        unit = _unit_of(rec)
        units.setdefault(unit, {"unit": unit,
                                "unit_name": str(rec.结算单元名称 or ""),
                                "config": "zero_salary_no_add" if unit in zero_codes else "normal",
                                "suggested": "skip" if unit in zero_codes else "declare",
                                "count": 0,
                                "income_total": 0.0,
                                "persons": {}})

    if not units:
        return {"pay_month": pay_month, "units": []}

    for rec in checked:
        if _unit_of(rec) in excl_codes:
            continue
        if calc_本期收入(rec) != 0:
            continue
        unit = _unit_of(rec)
        cert = _cert_of(rec)
        if not cert:
            continue
        u = units[unit]
        u["count"] += 1
        u["income_total"] += float(calc_本期收入(rec))
        p = u["persons"].setdefault(cert, {
            "cert_no": cert,
            "name": str(rec.姓名 or ""),
            "emp_no": str(rec.职工号 or ""),
            "salary_months": [],
            "income_total": 0.0,
        })
        p["salary_months"].append(rec.工资所属年月)
        p["income_total"] += float(calc_本期收入(rec))

    for unit, u in units.items():
        in_config = u["config"] == "zero_salary_no_add"
        for cert, p in u["persons"].items():
            p["salary_months"] = sorted(set(p["salary_months"]))
            p["income_total"] = round(p["income_total"], 2)
            if in_config:
                suggested, reason = "skip", "结算单元配置'工资为0不增员不报税'，默认不生成零申报"
            else:
                suggested, reason = "declare", "本期收入为0，默认生成零申报"
            override = overrides.get(cert)
            if override:
                default_chosen = override["mode"]
                persisted = True
            else:
                default_chosen = suggested
                persisted = False
            p["suggested"] = suggested
            p["reason"] = reason
            p["default_chosen"] = default_chosen
            p["persisted"] = persisted
        u["income_total"] = round(u["income_total"], 2)
        u["persons"] = sorted(u["persons"].values(),
                              key=lambda x: (x["cert_no"],))

    result_units = sorted(units.values(), key=lambda u: u["unit"])
    for u in result_units:
        unit_names = {int(c.get("unit", 0) or 0): str(c.get("unit_name", "") or "")
                      for c in combos}
        if not u["unit_name"]:
            u["unit_name"] = unit_names.get(u["unit"], "")
    return {"pay_month": pay_month, "units": result_units}


def filter_zero_records(records, zero_choices, excl_codes):
    """按用户零申报选择过滤记录: 本期收入=0 且用户选择 skip 的记录剔除。

    Records with 本期收入 != 0 are always kept; exclude_all units always dropped.
    zero_choices: {cert_no: 'declare'|'skip'} (面板确认后的完整选择)。
    未在 zero_choices 中的本期收入=0 记录: 保留 (用户未表态即生成零申报)。
    """
    skip_certs = {c for c, m in zero_choices.items() if m == "skip"}
    kept = []
    for rec in records:
        if _unit_of(rec) in excl_codes:
            continue
        if calc_本期收入(rec) == 0 and _cert_of(rec) in skip_certs:
            continue
        kept.append(rec)
    return kept


def filter_zero_dicts(records, zero_choices, excl_codes):
    """tc93_all/abnormal 字典记录版本的 filter_zero_records。"""
    skip_certs = {c for c, m in zero_choices.items() if m == "skip"}
    kept = []
    for rec in records:
        if _unit_of_dict(rec) in excl_codes:
            continue
        if _income_of_dict(rec) == 0 and _cert_of_dict(rec) in skip_certs:
            continue
        kept.append(rec)
    return kept