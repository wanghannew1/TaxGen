"""tax_zero.py - 零申报（工资为0人员是否生成零申报）建议与过滤

用户需求: 程序自动判定(工资为0不增员不报税)难以满足实际场景, 由用户在生成前
通过结算单元粒度 + 员工粒度的零申报建议挑选: 哪些人生成零申报, 哪些不生成。

候选来源 (2026-09-11 用户确认扩展):
- A1 工资单收入=0: TC93 有记录且公式计算本期收入=0 (报税公式逐条计算)
- A2 做了工资没发: TC93 有记录但 TC8M 无对应已发记录 (未发放无纳税义务)
- B  个税端在职无工资: 个税端名单 (境内/境外 xls, SQLite tax_roster) 在册
     无离职日期, 且本期无任何 TC93 工资记录 → 新签合同未做工资(B1) / 在册无痕迹(B2)
- B 类三分类 (2026-09-11 用户确认):
     b_outside  名单在册但系统查无此人 (TC90 无合同, 人工管理) → 默认不生成, 面板体现确认
     b_left     在系统但工资结束年月 (TC90.ATC90AV) 早于发放月 → 已离职, 默认不生成+建议减员
     b_roster   在系统且在册在职 → 维持 B1/B2 (默认 declare 保留在册)
- 在系统判定 (2026-09-11 用户确认): 以 TC90 有无合同为准 (既有 AC01 仅是个人基本信息,
     如电话等, 只能辅助查询, 不判定系统管理); 同一证件多份合同时以最后一份
     (开始日期 ATC90C 最大) 为准 —— 孙文强/杨冰 AC01 有主档但 TC90 无合同 → b_outside

口径 (用户确认):
- 判定: 单条记录本期收入=0 (报税公式逐条计算, 与生成文件"本期收入"列对应)
- 范围: 包含已被"工资为0不增员不报税"配置排除的人 (可反转恢复生成);
        完全排除单元 (exclude_all) 仍强制排除, 不进入候选
- 建议: 配置单元(38973/41100等 zero_salary_no_add=1) → 默认 skip (不生成), 含 B 类;
        其他单元 → 默认 declare (生成零申报);
        A2 未发放 → 默认 skip (无纳税义务); B 不在系统/已离职 → 默认 skip (面板确认);
        B 在册在职 → 默认 declare (保留在册)
- 持久化: config_db.zero_override, 用户"记住本次选择"后下次默认沿用
"""
from decimal import Decimal
from dataclasses import replace

from queries import (get_salary_records_by_combos, get_unpaid_salary_cert_months,
                     get_paid_units_in_month)
from config_db import get_zero_overrides, get_zero_salary_unit_codes, get_excluded_unit_codes
from templates_gen.formulas import calc_本期收入
from models import SalaryRecord

# 候选来源分类 (2026-09-11 用户确认, 理由列按分类显示)
CAT_A1_INCOME_ZERO = "a1_income_zero"   # 工资表公式计算本期收入=0
CAT_A2_UNPAID = "a2_unpaid"             # 做了工资没发 (TC8M 无发放)
CAT_B_ROSTER = "b_roster"               # 个税端在职无工资 (名单在册, 在职)
CAT_B_OUTSIDE = "b_outside"             # 名单在册但系统查无此人 (人工管理, 默认不生成)
CAT_B_LEFT = "b_left"                   # 在系统但工资结束年月早于发放月 (已离职, 默认不生成+建议减员)

# 单元内人员展示排序 (2026-09-11 用户确认): 名单在册最前, 不在系统最后
_CAT_SORT_ORDER = {
    CAT_B_ROSTER: 0,          # 名单在册 (在职) → 最前
    CAT_B_LEFT: 1,            # 名单在册·已离职
    CAT_A1_INCOME_ZERO: 2,    # 工资单收入=0
    CAT_A2_UNPAID: 3,         # 做了工资没发
    CAT_B_OUTSIDE: 4,         # 名单在册·不在系统 → 最后
}

REASON_A1 = "工资表按公式计算本期收入为0，默认生成零申报"
REASON_A1_CONFIG = "结算单元配置'工资为0不增员不报税'，默认不生成零申报"
REASON_A2 = "已做工资但未发放(TC8M无发放)，无纳税义务，默认不生成；若个税端需保留在册可改生成"
REASON_B1 = "个税端在职，本期新签合同未做工资，默认生成零申报保留在册"
REASON_B2 = "个税端在职，本期未做工资未发放未减员，默认生成零申报；若已离职请先办理减员"
REASON_B_OUTSIDE = "个税端在职但不在系统管理(人工管理)，默认不生成零申报；如需在个税端保留请确认生成"
REASON_B_LEFT = "工资结束年月{ym}早于所属年月{pay}，判定已离职，默认不生成零申报，建议办理减员"


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


def _month_end(pay_month: int) -> str:
    """发放月月末字符串 YYYY-MM-DD (名单离职日期判定)。"""
    from calendar import monthrange
    y, m = divmod(pay_month, 100)
    return f"{y:04d}-{m:02d}-{monthrange(y, m)[1]:02d}"


def _zero_salary_record(rec) -> SalaryRecord:
    """保留身份与单元信息, 金额字段全部清零 (A2 未发 declare → 零申报行)。"""
    return replace(rec,
                   应发工资=Decimal("0"), 实发工资=Decimal("0"), 个人所得税=Decimal("0"),
                   工资总额=Decimal("0"), 独生子女费=Decimal("0"), 采暖费=Decimal("0"),
                   奖金=Decimal("0"), 养老个人=Decimal("0"), 医疗个人=Decimal("0"),
                   失业个人=Decimal("0"), 公积金个人=Decimal("0"),
                   补缴及退款保险金额个人=Decimal("0"), 大病险个人=Decimal("0"),
                   补发3=Decimal("0"), 个人交纳现金=Decimal("0"),
                   个人其他调整=Decimal("0"), 个人欠款=Decimal("0"),
                   扣款大病险=Decimal("0"), 税后工会会费=Decimal("0"),
                   个人代理费=Decimal("0"), 意外险个人=Decimal("0"), 经济补偿金=Decimal("0"))


def build_zero_salary_suggestions(conn, pay_month, combos, roster=None, handler=""):
    """扫描候选零申报人员并分组, 候选来源 (2026-09-11 用户确认三来源):
    - A1 工资单收入=0: TC93 有记录且公式计算本期收入=0
    - A2 做了工资没发: TC93 有记录但 TC8M 无发放 (未发无纳税义务, 一律进候选)
    - B  个税端在职无工资: 名单 (config_db.tax_roster, 境内/境外) 在册无离职日期,
         本期无 TC93 工资记录。B 类三分类 (2026-09-11 用户确认):
         b_outside 系统查无此人 (AC01 无, 人工管理) → 默认不生成, 面板体现确认;
         b_left    在系统但工资结束年月 (TC90.ATC90AV) < 所属年月 → 已离职, 默认不生成+建议减员;
         b_roster  在系统且在册在职 → 新签合同未做工资(B1) / 在册无痕迹(B2)

    Args:
        conn: Oracle 连接 (只读)
        pay_month: 发放月份, 如 202606
        combos: 前端确认的组合列表, 每项含 unit/salary_month/seq 等
        roster: 个税端名单列表 (get_tax_roster()), None/空则不生成 B 来源
        handler: 经办人过滤 (2026-09-11 用户需求), 非空时 A/B 候选均只保留
            经办人匹配者: A 类由前端按 combo.handler 预过滤; B 类在此按
            handler(TC8M.AAE019) > make_handler(TC93.AAE019) > 合同经办人 链匹配,
            无经办人关联的 b_outside 一并排除

    Returns:
        dict: {
            "pay_month", "units": [
                {"unit", "unit_name", "config", "suggested", "count", "income_total",
                 "persons": [{"cert_no", "name", "emp_no", "salary_months",
                              "income_total", "suggested", "reason", "category",
                              "default_chosen", "persisted"}]}
            ]
        }
        category: a1_income_zero | a2_unpaid | b_roster (前端按分类展示理由)
    """
    handler_filter = (handler or "").strip()
    combo_set = {(int(c.get("unit", 0) or 0), int(c.get("salary_month", 0) or 0),
                  str(c.get("seq", "") or "")) for c in combos}
    salary_months = sorted({c[1] for c in combo_set})
    if not combo_set or not salary_months:
        return {"pay_month": pay_month, "units": []}

    records = get_salary_records_by_combos(conn, combos)
    checked = [r for r in records
               if (_unit_of(r), r.工资所属年月, r.当月批次) in combo_set]

    zero_codes = set(get_zero_salary_unit_codes())
    excl_codes = set(get_excluded_unit_codes())
    overrides = get_zero_overrides()
    # A2 未发判定精确到 (cert, 所属月): 同人 6 月已发、7 月未发时仅 7 月记录被标记
    unpaid_pairs = get_unpaid_salary_cert_months(conn, salary_months)
    # 发放月已发单元 (2026-09-11 用户确认规则): 单元在发放当月有任一发放 →
    # 该单元"做了没发"人员既不需要零申报也不按数额申报, 不进候选
    paid_units = get_paid_units_in_month(conn, pay_month)

    units = {}
    checked_certs = set()

    def _ensure_unit(unit, unit_name, config, suggested):
        return units.setdefault(unit, {"unit": unit,
                                       "unit_name": unit_name,
                                       "config": config,
                                       "suggested": suggested,
                                       "count": 0,
                                       "income_total": 0.0,
                                       "persons": {}})

    for rec in checked:
        if _unit_of(rec) in excl_codes:
            continue
        cert = _cert_of(rec)
        if not cert:
            continue
        checked_certs.add(cert)
        income = calc_本期收入(rec)
        if (cert, rec.工资所属年月) in unpaid_pairs:
            # 发放月已发单元: 该单元"做了没发"人员不零申报也不按数额申报
            if _unit_of(rec) in paid_units:
                continue
            category = CAT_A2_UNPAID
        elif income == 0:
            category = CAT_A1_INCOME_ZERO
        else:
            continue
        unit = _unit_of(rec)
        u = _ensure_unit(unit, str(rec.结算单元名称 or ""),
                         "zero_salary_no_add" if unit in zero_codes else "normal",
                         "skip" if unit in zero_codes else "declare")
        u["count"] += 1
        u["income_total"] += float(income)
        p = u["persons"].setdefault(cert, {
            "cert_no": cert,
            "name": str(rec.姓名 or ""),
            "emp_no": str(rec.职工号 or ""),
            "salary_months": [],
            "income_total": 0.0,
        })
        p["salary_months"].append(rec.工资所属年月)
        p["income_total"] += float(income)
        p["category"] = category

    if roster:
        month_end = _month_end(pay_month)
        b_certs = {}
        for p in roster:
            cert = str(p.get("cert_no") or "").strip().upper()
            if not cert or cert in checked_certs:
                continue
            leave = str(p.get("leave_date") or "").strip()
            if leave and leave <= month_end:
                continue
            b_certs[cert] = p
        if b_certs:
            from queries import get_person_tc90_info, get_person_system_info
            # TC90 一次全表合并: 在系统判定 + 合同单元/经办人 + 工资结束年月
            # (2026-09-12 性能优化: 原 3 个分批 IN 查询合并为单次全表 ~1.4s + 5 分钟缓存)
            tc90_info = get_person_tc90_info(conn)
            # B 类三分类 (2026-09-11 用户确认):
            # 不在系统 (TC90 无合同, 人工管理) → 默认不生成, 面板体现让用户确认;
            # 在系统但工资结束年月 (TC90.ATC90AV) < 发放月 → 已离职, 默认不生成+建议减员;
            # 在系统且在册在职 → 维持 B1/B2 (默认 declare 保留在册)
            # 在系统判定口径: TC90 有无合同为准 (AC01 仅个人基本信息, 不判定系统管理);
            # 同一证件多份合同时以最后一份 (ATC90C 最大) 为准
            in_system = set(tc90_info)
            # 在系统人员展示: 结算单元/最后发薪工资单/工资结束年月/经办人 (2026-09-11 用户需求)
            system_info = get_person_system_info(conn, list(in_system)) if in_system else {}
            y, m = divmod(max(salary_months), 100)
            hire_start = f"{y - 1:04d}-{m:02d}-01"  # 近12个月入职 → B1 新签合同未做工资
            for cert, p in b_certs.items():
                info = tc90_info.get(cert, {})
                unit = int(info.get("unit_code") or 0)
                unit_name = str(info.get("unit_name") or "")
                if unit in excl_codes:
                    continue
                if handler_filter:
                    # 经办人过滤: B 类候选无经办人关联 (b_outside) 或
                    # 经办人链 (TC8M 经办人 > TC93 做工资经办人 > 合同经办人)
                    # 不含过滤值时排除
                    si = system_info.get(cert, {})
                    h = (str(si.get("handler") or "")
                         or str(si.get("make_handler") or "")
                         or str((info.get("contract_handlers") or [""])[0] or ""))
                    if handler_filter not in h:
                        continue
                hire = str(p.get("hire_date") or "").strip()
                is_new = bool(hire) and hire >= hire_start
                u = _ensure_unit(unit, unit_name or "未关联结算单元",
                                 "zero_salary_no_add" if unit in zero_codes else "normal",
                                 "skip" if unit in zero_codes else "declare")
                u["count"] += 1
                p_entry = u["persons"].setdefault(cert, {
                    "cert_no": cert,
                    "name": str(p.get("name") or ""),
                    "emp_no": str(p.get("emp_no") or ""),
                    "salary_months": [],
                    "income_total": 0.0,
                })
                end_ym = int(info.get("salary_end_ym") or 0)
                if cert not in in_system:
                    # 不在系统管理 (人工管理): 默认不生成零申报, 面板体现由用户确认
                    p_entry["category"] = CAT_B_OUTSIDE
                elif end_ym and end_ym < min(salary_months):
                    # 工资结束年月(ATC90AV)早于所属年月 → 判定已离职: 默认不生成, 建议减员
                    p_entry["category"] = CAT_B_LEFT
                    p_entry["_end_ym"] = end_ym
                else:
                    p_entry["category"] = CAT_B_ROSTER
                    p_entry["_is_new"] = is_new
                if cert in in_system:
                    si = system_info.get(cert, {})
                    u_code = unit or int(si.get("unit_code") or 0)
                    u_name = unit_name or str(si.get("unit_name") or "")
                    handler = (str(si.get("handler") or "")
                               or str(si.get("make_handler") or "")
                               or str((info.get("contract_handlers") or [""])[0] or ""))
                    p_entry["_sys_info"] = {
                        "unit_code": u_code, "unit_name": u_name,
                        "last_pay_ym": int(si.get("last_pay_ym") or 0),
                        "pay_month": int(si.get("pay_month") or 0),
                        "batch": str(si.get("last_batch") or ""),
                        "end_ym": end_ym,
                        "handler": handler,
                    }

    if not units:
        return {"pay_month": pay_month, "units": []}

    for unit, u in units.items():
        in_config = u["config"] == "zero_salary_no_add"
        for cert, p in u["persons"].items():
            p["salary_months"] = sorted(set(p["salary_months"]))
            p["income_total"] = round(p["income_total"], 2)
            if p.get("suggested") is None:
                if p["category"] == CAT_A2_UNPAID:
                    suggested, reason = "skip", REASON_A2
                elif p["category"] == CAT_B_OUTSIDE:
                    suggested, reason = "skip", REASON_B_OUTSIDE
                elif p["category"] == CAT_B_LEFT:
                    suggested, reason = "skip", REASON_B_LEFT.format(
                        ym=p["_end_ym"], pay=min(salary_months))
                elif in_config:
                    suggested, reason = "skip", REASON_A1_CONFIG
                elif p["category"] == CAT_B_ROSTER:
                    if p.pop("_is_new", False):
                        suggested, reason = "declare", REASON_B1
                    else:
                        suggested, reason = "declare", REASON_B2
                else:
                    suggested, reason = "declare", REASON_A1
                p["suggested"] = suggested
                p["reason"] = reason
            override = overrides.get(cert)
            if override:
                default_chosen = override["mode"]
                persisted = True
            else:
                default_chosen = p["suggested"]
                persisted = False
            p["default_chosen"] = default_chosen
            p["persisted"] = persisted
            si = p.pop("_sys_info", None)
            if si is not None:
                parts = []
                if si["unit_name"]:
                    unit_label = (f"{si['unit_name']}"
                                  + (f"({si['unit_code']})" if si["unit_code"] else ""))
                    parts.append(f"结算单元:{unit_label}")
                if si["last_pay_ym"]:
                    batch = (f"批{si['batch']}" if si["batch"] else "")
                    pay_note = (f"({si['pay_month']}发)"
                                if si["pay_month"] and si["pay_month"] != si["last_pay_ym"]
                                else "")
                    parts.append(f"最后发薪:{si['last_pay_ym']}{batch}{pay_note}")
                if si["end_ym"]:
                    parts.append(f"工资结束:{si['end_ym']}")
                if si["handler"]:
                    parts.append(f"经办人:{si['handler']}")
                p["sys_info"] = "；".join(parts) if parts else "系统内无合同/工资记录"
        u["income_total"] = round(u["income_total"], 2)
        # 人员排序: 名单在册(b_roster)最前, 不在系统(b_outside)最后 (2026-09-11 用户确认)
        u["persons"] = sorted(u["persons"].values(),
                              key=lambda x: (_CAT_SORT_ORDER.get(x["category"], 9),
                                             x["cert_no"]))

    result_units = sorted(units.values(), key=lambda u: u["unit"])
    unit_names = {int(c.get("unit", 0) or 0): str(c.get("unit_name", "") or "")
                  for c in combos}
    for u in result_units:
        if not u["unit_name"]:
            u["unit_name"] = unit_names.get(u["unit"], "")
    return {"pay_month": pay_month, "units": result_units}


def build_roster_zero_records(conn, pay_month, roster, zero_choices, excl_codes,
                              checked_certs, salary_months=None):
    """名单在册无工资人员 (B 类) 被确认"生成" → 构造零申报 SalaryRecord 列表。

    注入正常工资生成: 收入/五险皆 0, 保留名单身份信息 (姓名/证件/工号),
    结算单元取合同单元 (TC90), 关联不到则 0。
    """
    declare_certs = {str(c).strip().upper() for c, m in (zero_choices or {}).items()
                     if m == "declare"}
    if not declare_certs or not roster:
        return []
    from queries import get_person_units_contract
    month_end = _month_end(pay_month)
    sm = sorted(salary_months or [pay_month])[0]
    checked = {str(c).strip().upper() for c in (checked_certs or set())}
    candidates = {}
    for p in roster:
        cert = str(p.get("cert_no") or "").strip().upper()
        if not cert or cert not in declare_certs or cert in checked:
            continue
        leave = str(p.get("leave_date") or "").strip()
        if leave and leave <= month_end:
            continue
        candidates[cert] = p
    if not candidates:
        return []
    contract_map = get_person_units_contract(conn, list(candidates))
    rows = []
    for cert, p in candidates.items():
        info = contract_map.get(cert, {})
        unit = int(info.get("unit_code") or 0)
        if unit in excl_codes:
            continue
        rows.append(SalaryRecord(
            职工号=str(p.get("emp_no") or "") or cert,
            姓名=str(p.get("name") or ""),
            身份证=cert,
            工资所属年月=sm,
            结算单元=unit,
            结算单元名称=str(info.get("unit_name") or ""),
            当月批次="",
            应发工资=Decimal("0"), 实发工资=Decimal("0"), 个人所得税=Decimal("0"),
            工资总额=Decimal("0"), 独生子女费=Decimal("0"), 采暖费=Decimal("0"),
            奖金=Decimal("0"), 养老个人=Decimal("0"), 医疗个人=Decimal("0"),
            失业个人=Decimal("0"), 公积金个人=Decimal("0"),
            补缴及退款保险金额个人=Decimal("0"), 大病险个人=Decimal("0"),
            补发3=Decimal("0"), 个人交纳现金=Decimal("0"),
            个人其他调整=Decimal("0"), 个人欠款=Decimal("0"),
            扣款大病险=Decimal("0"), 税后工会会费=Decimal("0"),
            个人代理费=Decimal("0"), 意外险个人=Decimal("0"), 经济补偿金=Decimal("0"),
        ))
    return rows


def filter_zero_records(records, zero_choices, excl_codes,
                        unpaid_pairs=None, paid_units=None):
    """按用户零申报选择过滤记录: 本期收入=0 且用户选择 skip 的记录剔除。

    Records with 本期收入 != 0 are always kept; exclude_all units always dropped.
    zero_choices: {cert_no: 'declare'|'skip'} (面板确认后的完整选择)。
    未在 zero_choices 中的本期收入=0 记录: 保留 (用户未表态即生成零申报)。

    unpaid_pairs (可选, A2 未发集合 {(cert, 所属月)}): 未发记录一律跳过
    (无纳税义务, 不进生成), 无论本期收入是否为 0; 但零申报面板确认 declare
    的未发人员保留为全零记录。同人 6 月已发、7 月未发时仅 7 月记录被跳过。
    paid_units (可选, 发放月已发单元集合): 该单元"做了没发"记录一律剔除,
    默认跳过逻辑也无条件剔除 (2026-09-11 用户确认: 不零申报也不按数额申报)。
    """
    skip_certs = {c for c, m in zero_choices.items() if m == "skip"}
    declare_certs = {c for c, m in zero_choices.items() if m == "declare"}
    kept = []
    for rec in records:
        if _unit_of(rec) in excl_codes:
            continue
        cert = _cert_of(rec)
        if unpaid_pairs and (cert, rec.工资所属年月) in unpaid_pairs:
            # 发放月已发单元的未发记录: 无论 declare 与否一律剔除
            if paid_units and _unit_of(rec) in paid_units:
                continue
            # A2 未发: 默认跳过 (无纳税义务); 仅当用户显式确认 declare 时保留为全零记录
            if cert in declare_certs:
                kept.append(_zero_salary_record(rec))
            continue
        if calc_本期收入(rec) == 0 and cert in skip_certs:
            continue
        kept.append(rec)
    return kept


def filter_zero_dicts(records, zero_choices, excl_codes,
                      unpaid_pairs=None, paid_units=None):
    """tc93_all/abnormal 字典记录版本的 filter_zero_records (A2 未发同理剔除)。"""
    skip_certs = {c for c, m in zero_choices.items() if m == "skip"}
    declare_certs = {c for c, m in zero_choices.items() if m == "declare"}
    kept = []
    for rec in records:
        if _unit_of_dict(rec) in excl_codes:
            continue
        cert = _cert_of_dict(rec)
        month = int(rec.get("ATC931") or 0)
        if unpaid_pairs and (cert, month) in unpaid_pairs:
            if paid_units and _unit_of_dict(rec) in paid_units:
                continue
            if cert in declare_certs:
                retained = dict(rec)
                for k in _MONEY_DICT_KEYS:
                    if k in retained:
                        retained[k] = Decimal("0")
                kept.append(retained)
            continue
        if _income_of_dict(rec) == 0 and cert in skip_certs:
            continue
        kept.append(rec)
    return kept


_MONEY_DICT_KEYS = (
    "ATC93AA", "ATC936", "ATC93BD", "ATC93BE", "ATC93X3", "ATC93E",
    "ATC93M", "ATC93AG", "ATC93N", "ATC93Q", "ATC93R",
    "ATC93W", "ATC93W1", "ATC93W2", "ATC93W3", "ATC93W4", "ATC93W21",
)