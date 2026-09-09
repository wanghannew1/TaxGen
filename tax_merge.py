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
from queries import get_salary_records
from filing_history import get_filing_map
from config_db import get_merge_overrides


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
            "pay_month", "candidates": [
                {"cert_no", "name", "emp_no", "units", "salary_months", "prev_month",
                 "prev_status", "prev_income", "prev_insurance", "be_flag",
                 "suggested", "reason", "confidence", "default_chosen", "persisted"}
            ]
        }
    """
    combo_set = {(int(c.get("unit", 0) or 0), int(c.get("salary_month", 0) or 0),
                  str(c.get("seq", "") or "")) for c in combos}
    salary_months = sorted({c[1] for c in combo_set})
    if not combo_set or not salary_months:
        return {"pay_month": pay_month, "prev_month": None, "candidates": []}

    records = []
    for sm in salary_months:
        records.extend(get_salary_records(conn, sm))
    checked = [r for r in records
               if (r.结算单元, r.工资所属年月, r.当月批次) in combo_set]
    if not checked:
        return {"pay_month": pay_month, "prev_month": None, "candidates": []}

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
    for cert, (pm, recs) in sorted(persons.items()):
        prev = prev_maps[pm].get(cert)
        status = _status_of(prev)
        prev_income = float(prev.get("income") or 0) if prev else 0.0
        prev_insurance = round(sum(float(prev.get(k) or 0) for k in
                                   ("pension", "medical", "unemployment", "housing")), 2) if prev else 0.0
        be_flag = any(float(r.补缴及退款保险金额个人 or 0) != 0 for r in recs)

        if be_flag:
            suggested, reason, confidence = "single", "本月ATC93BE≠0（借支/补缴）→ 三险不得翻倍", "high"
        elif status == "有报":
            suggested, reason, confidence = "single", "上月已报三险 → 单倍（避免重复扣除）", "high"
        elif status == "未找到":
            suggested, reason, confidence = "single", "上月未找到（历史0例翻倍）", "high"
        else:
            suggested, reason, confidence = "double", "上月零申报 → 可翻倍合并（61%实证，请确认）", "medium"

        override = overrides.get(cert)
        if override:
            default_chosen = override["mode"]
            persisted = True
        else:
            default_chosen = suggested
            persisted = False

        base = recs[0]
        candidates.append({
            "cert_no": cert,
            "name": str(base.姓名 or ""),
            "emp_no": str(base.职工号 or ""),
            "units": sorted({r.结算单元 for r in recs}),
            "salary_months": sorted({r.工资所属年月 for r in recs}),
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

    return {"pay_month": pay_month,
            "prev_month": min(prev_maps) if prev_maps else None,
            "candidates": candidates}