"""tax_merge.py - 三险一金合并规则（翻倍/单倍）判定建议

核心问题：同一发放月内同人跨多个所属月（双月同发/跨月补报）合并申报时，
三险一金应翻倍（上月三险并入本月）还是单倍（只报本月三险）？

数据依据：上月报送档（filing_record 税款计算，month=税款所属期）中该人状态
（见 docs/0申报三险一金合并规则分析.md 实证）：
- 上月有报（三险>0）→ 单倍（99.5%）
- 上月零申报（收入=0 三险=0）→ 翻倍（61%，其余39%不翻倍 → 需用户判断）
- 上月未找到 → 单倍（100%）
- 本月任何记录 ATC93BE≠0（借支/补缴缴费）→ 单倍（强制，BE 已在收入侧扣除）

用户确认后的选择可持久化（config_db.merge_override），下次默认沿用。
"""
from queries import get_salary_records_by_combos, get_unit_insurance_stats
from filing_history import get_filing_map
from config_db import get_merge_overrides
from templates_gen.formulas import calc_本期收入


# 主结算单元判定: 单元级属性 (该单元绝大多数人缴五险一金, 才作为人员分组归属)
MAIN_UNIT_MIN_PEOPLE = 5      # 该单元至少 5 人发薪才统计覆盖度, 避免 1-2 人偶然
MAIN_UNIT_INSURED_RATIO = 0.8  # 有保险人数占比 ≥80% 视为"绝大部分人缴五险一金"


def merge_records_by_person(records, by_pay_month: bool = False,
                            single_certs: set | None = None):
    """按人合并多条工资记录为一笔，基准取时间上最后一个批次(批次号最大、流水号最大)。

    by_pay_month=False: 按人+所属月份合并（现状，同人同月多笔合并，同一人可能多行）。
    by_pay_month=True:  按人+发放月份合并（同一发放月份内每人一行，跨所属月份的收入/五险一金/个税全部合计）。
                        仅适用于组合确认流程——所有组合共享同一发放月份(TC8M.ATC8G7)。
    single_certs: 三险一金单倍处理的证件号集合（用户确认"不翻倍"的人员）。
                  这些人的收入/个税仍跨所属月累加，但养老/医疗/失业/公积金只取基准记录，
                  不累加其他所属月（上月在旧档已报过的三险不再并入本月，避免重复扣除）。
    """
    from copy import deepcopy
    single_certs = single_certs or set()
    group_key = (lambda rec: (rec.职工号,)) if by_pay_month \
        else (lambda rec: (rec.职工号, rec.工资所属年月))
    groups = {}
    for rec in records:
        groups.setdefault(group_key(rec), []).append(rec)

    merged = []
    for key, recs in groups.items():
        if by_pay_month:
            base = max(recs, key=lambda r: r.tc930_id)
        else:
            base = max(recs, key=lambda r: (int(r.当月批次 or 0), r.tc930_id))
        m = deepcopy(base)
        single = by_pay_month and m.身份证 in single_certs
        for rec in recs:
            if rec is base:
                continue
            m.应发工资 += rec.应发工资
            m.实发工资 += rec.实发工资
            m.个人所得税 += rec.个人所得税
            m.工资总额 += rec.工资总额
            m.独生子女费 += rec.独生子女费
            m.采暖费 += rec.采暖费
            m.奖金 += rec.奖金
            if not single:
                m.养老个人 += rec.养老个人
                m.医疗个人 += rec.医疗个人
                m.失业个人 += rec.失业个人
                m.公积金个人 += rec.公积金个人
            m.补缴及退款保险金额个人 += rec.补缴及退款保险金额个人
            m.大病险个人 += rec.大病险个人
            m.补发3 += rec.补发3
            m.个人交纳现金 += rec.个人交纳现金
            m.个人其他调整 += rec.个人其他调整
            m.个人欠款 += rec.个人欠款
            m.扣款大病险 += rec.扣款大病险
            m.税后工会会费 += rec.税后工会会费
            m.个人代理费 += rec.个人代理费
            m.意外险个人 += rec.意外险个人
            m.经济补偿金 += rec.经济补偿金
        merged.append(m)
    return merged


def _prev_month_of(salary_months):
    """候选人员跨所属月中的最早月 = 其"上月工资"所在档（税款所属期）。"""
    return min(salary_months)


def _main_unit_of(recs, main_units):
    """判定跨单元发放人员归属的主结算单元。

    主结算单元是**单元级属性**（用户 202608 澄清）：不判断本人当月是否在某单元
    缴五险一金，而是看该结算单元在历史上是否绝大多数人缴五险一金；人员归属 =
    本人发薪所涉单元 ∩ 主单元集合。若本人所有发薪单元都不是主单元
    （如只在奖金/补贴单元发薪）→ 回落第一个发薪的结算单元（最早所属月，
    平局取最小单元代码）。
    """
    unit_first_month = {}
    for r in recs:
        u = int(r.结算单元 or 0)
        m = int(r.工资所属年月 or 0)
        if u not in unit_first_month or m < unit_first_month[u]:
            unit_first_month[u] = m
    if not unit_first_month:
        return 0
    pool = {u for u in unit_first_month if u in main_units}
    if not pool:
        pool = set(unit_first_month)  # 全部非主单元 → 回落最早发薪单元
    return min(pool, key=lambda u: (unit_first_month[u], u))


def _slips_of(recs):
    """某人全部工资单明细（跨单元/月/批次），按所属月升序，附本期收入与三险一金金额。

    is_base: 该工资单是否为基准记录(tc930_id 最大)——单倍时三险一金只取基准记录，
             与 merge_records_by_person(single_certs) 口径一致。
    """
    base = max(recs, key=lambda r: r.tc930_id)
    slips = []
    for r in recs:
        pension = float(r.养老个人 or 0)
        medical = float(r.医疗个人 or 0)
        unemp = float(r.失业个人 or 0)
        housing = float(r.公积金个人 or 0)
        slips.append({
            "unit": int(r.结算单元 or 0),
            "unit_name": str(r.结算单元名称 or ""),
            "salary_month": int(r.工资所属年月 or 0),
            "seq": str(r.当月批次 or ""),
            "tc930_id": r.tc930_id,
            "income": round(float(calc_本期收入(r)), 2),
            "pension": round(pension, 2),
            "medical": round(medical, 2),
            "unemployment": round(unemp, 2),
            "housing": round(housing, 2),
            "insurance": round(pension + medical + unemp + housing, 2),
            "is_base": r is base,
        })
    slips.sort(key=lambda s: (s["salary_month"], s["unit"], s["seq"]))
    return slips


def _status_of(prev):
    """判定上月档案状态: '有报' | '零申报' | '未找到'。"""
    if prev is None:
        return "未找到"
    insurance = sum(float(prev[k] or 0) for k in ("pension", "medical", "unemployment", "housing"))
    income = float(prev.get("income") or 0)
    if insurance > 0:
        return "有报"
    if income == 0:
        return "零申报"
    return "有报"  # 收入>0 但三险=0（异常档，按已报处理，不虚构三险）


def build_merge_suggestions(conn, pay_month, combos):
    """扫描指定发放月已确认组合中的跨月合并人员，给出翻倍/单倍判定建议。

    Args:
        conn: Oracle 连接（只读）
        pay_month: 发放月份（TC8M.ATC8G7），如 202606
        combos: 前端确认的组合列表，每项含 unit/salary_month/seq 等

    Returns:
        dict: {
            "pay_month", "prev_month", "candidates": [
                {"cert_no", "name", "emp_no", "units", "unit_names", "salary_months",
                 "prev_month", "prev_status", "prev_income", "prev_insurance", "be_flag",
                 "suggested", "reason", "confidence", "default_chosen", "persisted"}
            ],
            "work_sheets": [   # 按主结算单元分组的确认工作单, 供前端分组批量操作
                {"unit", "unit_name", "count", "persons": [{...candidate}]}
            ]
        }
    """
    combo_set = {(int(c.get("unit", 0) or 0), int(c.get("salary_month", 0) or 0),
                  str(c.get("seq", "") or "")) for c in combos}
    salary_months = sorted({c[1] for c in combo_set})
    if not combo_set or not salary_months:
        return {"pay_month": pay_month, "prev_month": None, "candidates": [],
                "work_sheets": []}

    records = get_salary_records_by_combos(conn, combos)
    checked = [r for r in records
               if (r.结算单元, r.工资所属年月, r.当月批次) in combo_set]
    if not checked:
        return {"pay_month": pay_month, "prev_month": None, "candidates": [],
                "work_sheets": []}

    # 主结算单元判定是单元级属性: 该单元在所属月范围内绝大多数人缴五险一金。
    # 人员归属 = 本人发薪单元 ∩ 主单元集合; 全部非主单元 → 回落最早发薪单元。
    stats = get_unit_insurance_stats(conn, sorted({r.结算单元 for r in checked}),
                                     salary_months)
    main_units = {u for u, s in stats.items()
                  if s["people"] >= MAIN_UNIT_MIN_PEOPLE
                  and s["insured"] / s["people"] >= MAIN_UNIT_INSURED_RATIO}

    by_person = {}
    for r in checked:
        key = r.身份证 or r.职工号
        by_person.setdefault(key, []).append(r)

    prev_months = set()
    persons = {}
    for cert, recs in by_person.items():
        months = sorted({r.工资所属年月 for r in recs})
        if len(months) < 2:
            continue  # 不跨所属月 → 无翻倍/单倍问题
        pm = _prev_month_of(months)
        prev_months.add(pm)
        persons[cert] = (pm, recs)

    prev_maps = {pm: get_filing_map(pm, "税款计算") for pm in prev_months}
    overrides = get_merge_overrides()

    candidates = []
    work_sheets = {}
    for cert, (pm, recs) in sorted(persons.items()):
        prev = prev_maps[pm].get(cert)
        status = _status_of(prev)
        prev_income = float(prev.get("income") or 0) if prev else 0.0
        prev_insurance = round(sum(float(prev.get(k) or 0) for k in
                                   ("pension", "medical", "unemployment", "housing")), 2) if prev else 0.0
        be_flag = any(float(r.补缴及退款保险金额个人 or 0) != 0 for r in recs)

        if be_flag:
            suggested, reason, confidence = "single", "本月ATC93BE≠0（借支/补缴）→ 只报当月三险（不合并上报）", "high"
        elif status == "有报":
            suggested, reason, confidence = "single", "上月已报三险 → 只报当月（避免重复扣除）", "high"
        elif status == "未找到":
            suggested, reason, confidence = "single", "上月未找到 → 只报当月（历史0例合并上报）", "high"
        else:
            suggested, reason, confidence = "double", "上月零申报 → 多笔三险合并上报（61%实证，请确认）", "medium"

        override = overrides.get(cert)
        if override:
            default_chosen = override["mode"]
            persisted = True
        else:
            default_chosen = suggested
            persisted = False

        base = recs[0]
        unit_names = {str(r.结算单元): str(r.结算单元名称 or "") for r in recs
                      if (r.结算单元, r.工资所属年月, r.当月批次) in combo_set}
        main_unit = _main_unit_of(recs, main_units)
        slips = _slips_of(recs)
        # 合并金额口径 (与 merge_records_by_person 完全一致):
        # 收入(本期收入)跨月总是全加; 三险一金 翻倍=各月全加, 单倍=只取基准记录(tc930_id 最大)。
        income_total = round(sum(float(calc_本期收入(r)) for r in recs), 2)
        insurance_double = round(sum(s["insurance"] for s in slips), 2)
        insurance_single = next(s["insurance"] for s in slips if s["is_base"])
        candidates.append({
            "cert_no": cert,
            "name": str(base.姓名 or ""),
            "emp_no": str(base.职工号 or ""),
            "units": sorted(int(u) for u in unit_names),
            "unit_names": unit_names,
            "salary_months": sorted({r.工资所属年月 for r in recs}),
            "main_unit": main_unit,
            "main_unit_name": unit_names.get(str(main_unit), ""),
            "slips": slips,
            "income_total": income_total,
            "insurance_double": insurance_double,
            "insurance_single": insurance_single,
            "prev_month": pm,
            "prev_status": status,
            "prev_income": prev_income,
            "prev_insurance": prev_insurance,
            "be_flag": be_flag,
            "suggested": suggested,
            "reason": reason,
            "confidence": confidence,
            "default_chosen": default_chosen,
            "persisted": persisted,
        })

    # 两级分组: 主结算单元大组(按五险一金判定) → 组内每人完整列出发薪工资单明细。
    # 每人只归属一个主单元, 全局唯一不重复。
    groups = {}
    for c in candidates:
        g = groups.setdefault(c["main_unit"], {
            "unit": c["main_unit"],
            "unit_name": c["main_unit_name"],
            "persons": [],
        })
        g["persons"].append(c)
    for g in groups.values():
        g["persons"] = sorted(g["persons"], key=lambda p: (p["name"], p["cert_no"]))
    sheet_list = [groups[u] for u in sorted(groups, key=lambda u: (groups[u]["unit_name"], u))]
    for g in sheet_list:
        g["count"] = len(g["persons"])

    return {"pay_month": pay_month,
            "prev_month": min(prev_maps) if prev_maps else None,
            "candidates": candidates,
            "work_sheets": sheet_list}