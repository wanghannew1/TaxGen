"""tax_forecast.py - 报税预判：基于历史申报规律预判下一次报税

数据口径（2026-11 修正）
------------------------
- 个税端「税款所属期」= 发放月（TC8M.ATC8G7），不是工资所属月（ATC931）。
  config_db.social_security_history.salary_month 字段沿用旧名，实为申报/发放月。
- 历史样本：social_security_history（已导入的个税端税款计算），默认回看 6 个月。
- 系统数据：Oracle 只读。发放月组合 = get_suggestions（TC8M 已确认发放 ATC8M3=2），
  记录 = get_salary_records_by_combos，本期收入用 calc_本期收入。

预判内容
--------
1. 合并预判（三险一金）：跨工资所属月人员按月/多月/不报。规则引擎沿用
   tax_merge.build_merge_suggestions，但"上月档案"改为上一个申报月，并改用
   social_security_history 作为历史来源（原 filing_record 库通常为空）。
2. 零申报预判：A1 工资为0 / A2 做了未发 / B 名单在册 / C 合同新入职 /
   A2次月发放 / A3无批次（复用 tax_zero），叠加历史规律：
   上月申报状态、近 N 月零申报次数、所属单位历史零申报率。
3. 历史在册无工资：上期个税端在册、本期系统无工资记录的人员，预测是否零申报。
4. 主结算单元归属：多结算单元发薪人员，按单元五险一金覆盖度预测归属，
   并与历史申报单位（个税端备注）比对。

历史回测
--------
对过去月份用"该月之前"的历史数据生成预判，与个税端实际申报对比：
- 合并：实际三险一金 ≈ 单月/多月/0 哪个口径；
- 零申报：实际是否出现在个税端（收入≈0 视为申报）；
- 主单元：实际申报备注单位与预测单位是否一致。
"""
import time
from collections import defaultdict
from decimal import Decimal

TOLERANCE = Decimal("0.01")
HISTORY_LOOKBACK = 6        # 用于预判的历史月数
UNIT_HISTORY_LOOKBACK = 3   # 单位零申报率回看月数
_CACHE = {}
_CACHE_TTL = 600
_INDEX_CACHE = {"ts": 0.0, "data": None}
_INDEX_TTL = 60

CATEGORY_LABELS = {
    "a1_income_zero": "工资公式收入=0",
    "a2_unpaid": "做了未发",
    "b_roster": "名单在册",
    "b_outside": "名单在册·不在系统",
    "b_left": "名单在册·已离职",
    "c1_contract_day1": "合同月初新入职",
    "c2_contract_mid": "合同月中新入职",
    "c3_contract_hist": "合同新入职·有历史发放",
    "a2_next_month": "次月/未来发放",
    "a3_no_batch": "做了未发(无批次)",
    "hist_roster": "历史在册无工资",
}

MERGE_LABELS = {"single": "单月", "double": "多月", "skip": "不报"}
ZERO_LABELS = {"declare": "申报", "skip": "不申报"}
STATUS_LABELS = {"有收入": "有收入", "零申报": "零申报", "未出现": "未出现"}


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")


def _norm_unit(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return ""
    for sep in ("-", "（", "("):
        idx = s.find(sep)
        if idx > 0:
            s = s[:idx]
    s = s.strip()
    if s.endswith("B"):
        s = s[:-1].strip()
    return s


# ---------------------------------------------------------------------------
# 历史申报索引
# ---------------------------------------------------------------------------

def _filing_index(refresh: bool = False) -> dict:
    """从 social_security_history 建立历史索引。

    Returns:
        {
          "by_month": {YYYYMM: {cert: {name, income, insurance, pension, medical,
                                       unemployment, housing, tax, unit, raw_unit}}},
          "unit_stats": {YYYYMM: {norm_unit: {people, zero, income}}},
          "months": [升序月份],
        }
    """
    now = time.time()
    cached = _INDEX_CACHE["data"]
    if cached is not None and not refresh and now - _INDEX_CACHE["ts"] < _INDEX_TTL:
        return cached

    from config_db import get_db
    conn = get_db()
    rows = conn.execute(
        '''SELECT salary_month, 证件号码, 姓名, 本期收入,
                  本期基本养老保险费, 本期基本医疗保险费, 本期失业保险费,
                  本期住房公积金, "应补(退)税额", 备注
           FROM social_security_history
           WHERE 所得项目 IS NULL OR 所得项目 = '' OR 所得项目 = '正常工资薪金'
           ORDER BY salary_month, id''').fetchall()
    conn.close()

    by_month = {}
    unit_stats = {}
    for r in rows:
        month = int(r[0] or 0)
        cert = str(r[1] or "").strip().upper()
        if not month or not cert:
            continue
        income = _dec(r[3])
        insurance = (_dec(r[4]) + _dec(r[5]) + _dec(r[6]) + _dec(r[7]))
        rec = {
            "name": str(r[2] or "").strip(),
            "income": income,
            "pension": _dec(r[4]),
            "medical": _dec(r[5]),
            "unemployment": _dec(r[6]),
            "housing": _dec(r[7]),
            "insurance": insurance,
            "tax": _dec(r[8]),
            "raw_unit": str(r[9] or "").strip(),
            "unit": _norm_unit(str(r[9] or "")),
        }
        by_month.setdefault(month, {})[cert] = rec
        u = unit_stats.setdefault(month, {}).setdefault(rec["unit"], {
            "people": 0, "zero": 0, "income": Decimal("0")})
        u["people"] += 1
        u["income"] += income
        if abs(income) <= TOLERANCE:
            u["zero"] += 1

    data = {
        "by_month": by_month,
        "unit_stats": unit_stats,
        "months": sorted(by_month),
    }
    _INDEX_CACHE["ts"] = now
    _INDEX_CACHE["data"] = data
    return data


def _history_context(before_month: int) -> dict:
    """取 before_month 之前最近 N 个申报月（不含当月）作为预判历史窗口。"""
    idx = _filing_index()
    months = [m for m in idx["months"] if m < int(before_month)][-HISTORY_LOOKBACK:]
    return {
        "months": months,
        "prev_month": months[-1] if months else None,
        "by_month": {m: idx["by_month"][m] for m in months},
        "unit_stats": {m: idx["unit_stats"][m] for m in months},
    }


def _filing_map(month: int) -> dict:
    """tax_merge 历史档案 provider：返回 {cert_no: {income, pension, ...}}。"""
    if not month:
        return {}
    data = _filing_index()["by_month"].get(int(month), {})
    result = {}
    for cert, rec in data.items():
        result[cert] = {
            "income": rec["income"],
            "pension": rec["pension"],
            "medical": rec["medical"],
            "unemployment": rec["unemployment"],
            "housing": rec["housing"],
            "tax_due": rec["tax"],
            "remark": rec["raw_unit"],
        }
    return result


def _person_history(ctx: dict, cert: str) -> list:
    """某人历史窗口内的申报记录，按月份升序。"""
    out = []
    for m in ctx["months"]:
        rec = ctx["by_month"][m].get(cert)
        if rec:
            out.append((m, rec))
    return out


def _prev_status(ctx: dict, cert: str):
    pm = ctx.get("prev_month")
    if not pm:
        return "未出现", None
    rec = ctx["by_month"][pm].get(cert)
    if rec is None:
        return "未出现", None
    if abs(rec["income"]) <= TOLERANCE and rec["insurance"] <= TOLERANCE:
        return "零申报", rec
    return "有收入", rec


def _unit_zero_rate(ctx: dict, unit_name: str):
    norm = _norm_unit(unit_name)
    if not norm:
        return None
    total = zero = 0
    for m in ctx["months"][-UNIT_HISTORY_LOOKBACK:]:
        s = ctx["unit_stats"][m].get(norm)
        if s:
            total += s["people"]
            zero += s["zero"]
    return (zero / total) if total else None


def _history_note(ctx: dict, cert: str, unit_name: str = "") -> dict:
    hist = _person_history(ctx, cert)
    prev_status, prev_rec = _prev_status(ctx, cert)
    zero_months = sum(1 for _m, r in hist
                      if abs(r["income"]) <= TOLERANCE and r["insurance"] <= TOLERANCE)
    last_unit = hist[-1][1]["raw_unit"] if hist else ""
    unit_rate = _unit_zero_rate(ctx, unit_name or last_unit)
    parts = [f"近{len(hist)}月申报{len(hist)}次（零申报{zero_months}次）",
             f"上月{STATUS_LABELS.get(prev_status, prev_status)}"]
    if unit_rate is not None:
        parts.append(f"单位历史零申报率{unit_rate:.0%}")
    return {
        "history_months": len(hist),
        "history_zero_months": zero_months,
        "prev_filing_status": prev_status,
        "prev_filing_income": float(prev_rec["income"]) if prev_rec else None,
        "history_unit": last_unit,
        "unit_zero_rate": round(unit_rate, 4) if unit_rate is not None else None,
        "history_note": "；".join(parts),
    }


# ---------------------------------------------------------------------------
# 预判：合并 / 零申报 / 主结算单元
# ---------------------------------------------------------------------------

def _candidate_month_incomes(c: dict) -> dict:
    """候选人各工资所属月的本期收入合计（用于与上月申报收入比对）。"""
    inc = {}
    for sl in c.get("slips") or []:
        sm = int(sl.get("salary_month") or 0)
        if not sm:
            continue
        inc[sm] = round(inc.get(sm, 0.0) + float(sl.get("income") or 0), 2)
    return inc


def _empirical_merge_rule(c: dict):
    """按"上月申报收入覆盖了本次哪几个所属月"判定单月/多月。

    回测（202604-202609，2050 个可比候选）合并准确率约 95%：
    - 上月申报收入能匹配本次部分所属月 → 剩余月份才是新增；
      新增 1 个月 → 单月，新增 >=2 个月 → 多月；
    - 匹配不上且两个所属月不相邻（如上月已报 5 月、本次发 5+7 月）
      且上月有申报 → 只报最近单月；
    - 上月零申报/未找到 → 多月合并。
    """
    months = sorted(int(m) for m in (c.get("salary_months") or []) if m)
    if len(months) < 2:
        return None
    raw_prev = c.get("prev_status")
    prev_income = float(c.get("prev_income") or 0)
    if raw_prev in ("零申报", "未找到") or prev_income <= 0:
        return ("double", "high", "history_double",
                "历史规律：上月零申报/无申报记录 → 倾向多月合并上报（实证约 90%）")
    if len(months) > 12:
        return ("double", "medium", "many_months",
                "本次合并涉及月份较多 → 倾向多月合并上报")

    inc = _candidate_month_incomes(c)
    matches = []
    for mask in range(1, 1 << len(months)):
        total = sum(inc.get(m, 0) for i, m in enumerate(months) if mask >> i & 1)
        if abs(total - prev_income) <= 0.02:
            matches.append(mask)
    if not matches:
        if len(months) == 2 and months[1] - months[0] > 1 and raw_prev == "有报":
            return ("single", "high", "gap_single",
                    f"两个所属月不相邻（{months[0]}、{months[1]}），"
                    "历史同类多只报最近单月")
        return ("double", "medium", "prev_income_no_match",
                "上月申报收入无法与本次所属月对应 → 倾向多月合并上报")

    latest_i = len(months) - 1
    mask = max(matches, key=lambda m: (bin(m).count("1"), (m >> latest_i) & 1))
    reported = [m for i, m in enumerate(months) if mask >> i & 1]
    new = [m for i, m in enumerate(months) if not (mask >> i & 1)]
    shown_r = "、".join(str(m) for m in reported) or "无"
    shown_n = "、".join(str(m) for m in new) or "无"
    if len(new) >= 2:
        return ("double", "high", "new_months_double",
                f"上月申报已覆盖{shown_r}，本次新增{shown_n}共{len(new)}个月 → 多月合并")
    if len(new) == 1:
        return ("single", "medium", "new_month_single",
                f"上月申报已覆盖{shown_r}，仅{shown_n}新增 → 按单月报（避免重复扣除）")
    if c.get("can_skip"):
        return ("skip", "medium", "already_reported_skip",
                f"上月申报已覆盖本次全部所属月（{shown_r}）且当月无三险 → 倾向不报")
    return ("single", "medium", "already_reported_single",
            f"上月申报已覆盖本次全部所属月（{shown_r}）→ 按单月报")


def _predict_merge_candidate(c: dict, ctx: dict) -> dict:
    """在 tax_merge 建议之上，用实证历史规律给出最终合并预判。

    回测口径（202604-202609 实证，合并准确率约 95%）：
    - 用上月申报收入反推已申报的工资所属月，剩余月份为本次新增；
      新增 1 个月 → 单月，新增 >=2 个月 → 多月；
    - 两个所属月不相邻且上月有申报 → 历史上多只报最近单月；
    - 单月==多月（当月无三险、只有历史月三险）：
      上月零申报 → 实际多为不报；上月有收入 → 实际多为单月。
    - ATC93BE 标记在实证中未显示必须单月，仅作提示，不再强制覆盖历史规律。
    """
    out = dict(c)
    hn = _history_note(ctx, c["cert_no"], c.get("main_unit_name", ""))
    prev_status = hn["prev_filing_status"]
    pattern = c.get("pay_pattern") or "normal"
    equal = bool(c.get("single_equals_double"))
    can_skip = bool(c.get("can_skip"))
    be_flag = bool(c.get("be_flag"))

    if c.get("persisted"):
        predicted = c.get("default_chosen") or "single"
        confidence, rule = "high", "persisted"
        reason = "沿用用户已保存的选择"
    elif pattern == "pay_lag":
        predicted, confidence, rule = "single", "high", "pay_pattern"
        reason = "该单元已标记压月发 → 按最近三险月报单月"
    elif pattern == "bimonthly":
        predicted, confidence, rule = "double", "high", "pay_pattern"
        reason = "该单元已标记双月发 → 多月合并上报"
    elif equal and can_skip:
        if prev_status == "零申报":
            predicted, confidence, rule = "skip", "medium", "history_zero_skip"
            reason = "历史规律：上月零申报且当月无三险，倾向不报（避免重复/无效三险）"
        elif prev_status == "有收入":
            predicted, confidence, rule = "single", "medium", "history_single"
            reason = "历史规律：上月有申报收入，倾向按单月报（避免重复扣除）"
        else:
            predicted, confidence, rule = "single", "low", "history_default"
            reason = "历史无申报记录，默认按单月报"
    else:
        empirical = _empirical_merge_rule(c)
        if empirical:
            predicted, confidence, rule, reason = empirical
        elif prev_status == "零申报":
            predicted, confidence, rule = "double", "high", "history_double"
            reason = "历史规律：上月零申报 → 倾向多月合并上报"
        elif prev_status == "有收入":
            predicted, confidence, rule = "single", "medium", "history_single"
            reason = "历史规律：上月有申报收入 → 倾向单月（避免重复扣除）"
        else:
            predicted, confidence, rule = "single", "low", "history_default"
            reason = "历史无申报记录，默认按单月报"

    if be_flag and not c.get("persisted") and rule not in ("pay_pattern",):
        reason += "；本月有借支/补缴标记（ATC93BE≠0），历史样本未显示必须单月"

    out.update({
        "predicted": predicted,
        "default_chosen": predicted,
        "confidence": confidence,
        "prediction_rule": rule,
        "prediction_reason": reason,
        "reason": reason,
        "suggested_label": MERGE_LABELS.get(c.get("suggested"), c.get("suggested")),
        "default_label": MERGE_LABELS.get(predicted, predicted),
    })
    out.update(hn)
    return out


def _predict_zero_person(p: dict, unit: dict, ctx: dict) -> dict:
    """在 tax_zero 规则建议之上叠加历史规律，返回最终预判。"""
    cert = str(p.get("cert_no") or "").strip().upper()
    cat = p.get("category") or ""
    hn = _history_note(ctx, cert, unit.get("unit_name", ""))
    base_suggested = p.get("suggested") or "declare"
    base_reason = p.get("reason") or ""
    suggested = base_suggested
    reason = base_reason
    confidence = "medium"
    rule = "existing"
    zero_history = (hn["prev_filing_status"] == "零申报"
                    or hn["history_zero_months"] >= 1)
    unit_rate = hn["unit_zero_rate"]

    if p.get("persisted"):
        suggested = p.get("default_chosen") or base_suggested
        confidence = "high"
        rule = "persisted"
        reason = "沿用用户已保存的选择；" + base_reason
    elif unit.get("config") == "zero_salary_no_add":
        suggested = "skip"
        confidence = "high"
        rule = "unit_config"
        reason = "结算单元已配置工资为0不增员不报税；" + base_reason
    elif cat in ("b_outside", "b_left"):
        suggested = "skip"
        confidence = "high" if hn["history_months"] else "medium"
        rule = "history_left"
        reason = "历史规律：离职/不在系统人员通常不申报；" + base_reason
    elif cat == "b_roster":
        suggested = "declare"
        confidence = "high" if zero_history else "medium"
        rule = "roster"
        reason = "个税端名单在册，保留申报；" + base_reason
    elif cat in ("c1_contract_day1", "c2_contract_mid", "c3_contract_hist",
                 "a2_next_month", "a3_no_batch"):
        if zero_history:
            suggested = "declare"
            confidence = "high" if (hn["history_zero_months"] >= 2
                                    or hn["prev_filing_status"] == "零申报") else "medium"
            rule = "history_zero"
            reason = (f"历史规律：近{hn['history_months']}月零申报"
                      f"{hn['history_zero_months']}次、上月{hn['prev_filing_status']}，"
                      "倾向零申报保留在册")
        elif unit_rate is not None and unit_rate >= 0.2:
            suggested = "declare"
            confidence = "medium"
            rule = "unit_history"
            reason = f"历史规律：所属单位零申报率{unit_rate:.0%}，倾向零申报"
        else:
            suggested = "skip"
            confidence = "medium" if unit_rate == 0 else "low"
            rule = "history_absent"
            reason = ("历史规律：无零申报惯性且单位零申报率低，倾向不申报；"
                      + base_reason)
    elif cat in ("a1_income_zero", "a2_unpaid"):
        if zero_history:
            suggested = "declare"
            confidence = "high" if (hn["history_zero_months"] >= 2
                                    or hn["prev_filing_status"] == "零申报") else "medium"
            rule = "history_zero"
            reason = (f"历史规律：近{hn['history_months']}月零申报"
                      f"{hn['history_zero_months']}次、上月{hn['prev_filing_status']}，"
                      "沿续零申报保留在册")
        elif unit_rate is not None and unit_rate >= 0.2:
            suggested = "declare"
            confidence = "medium"
            rule = "unit_history"
            reason = f"历史规律：所属单位零申报率{unit_rate:.0%}，倾向零申报"
        elif unit_rate == 0 and hn["history_months"] == 0:
            suggested = "skip"
            confidence = "medium"
            rule = "unit_history"
            reason = "历史规律：所属单位从不零申报且本人无申报历史，倾向不申报"
        elif hn["prev_filing_status"] == "未出现" and hn["history_months"] == 0:
            suggested = "skip"
            confidence = "medium"
            rule = "history_absent"
            reason = "历史规律：近月个税端未出现，倾向不申报"
        else:
            confidence = "low"
            rule = "existing"
            reason = "历史样本不足，沿用现有规则：" + base_reason
    else:
        confidence = "low"
        reason = "历史样本不足，沿用现有规则：" + base_reason

    out = dict(p)
    out.update({
        "category_label": CATEGORY_LABELS.get(cat, cat),
        "unit": int(unit.get("unit") or 0),
        "unit_name": unit.get("unit_name") or "",
        "unit_config": unit.get("config") or "normal",
        "suggested": suggested,
        "default_chosen": suggested,
        "reason": reason,
        "confidence": confidence,
        "prediction_rule": rule,
    })
    out.update(hn)
    return out


def _predict_zero_rows(zero_res: dict, ctx: dict) -> list:
    rows = []
    for unit in zero_res.get("units") or []:
        for p in unit.get("persons") or []:
            rows.append(_predict_zero_person(p, unit, ctx))
    rows.sort(key=lambda r: (
        {"high": 0, "medium": 1, "low": 2}.get(r.get("confidence"), 3),
        0 if r.get("default_chosen") == "declare" else 1,
        r.get("unit") or 0, r.get("cert_no") or ""))
    return rows


def _history_roster_rows(conn, ctx: dict, pay_month: int, records: list,
                         existing_certs: set) -> list:
    """上期个税端在册、本期系统无工资记录的人员 → 零申报预判（不注入生成）。"""
    prev_month = ctx.get("prev_month")
    if not prev_month:
        return []
    payroll_certs = {str(r.身份证 or r.职工号 or "").strip().upper()
                     for r in records if (r.身份证 or r.职工号)}
    try:
        from queries import get_person_tc90_info
        tc90 = get_person_tc90_info(conn)
    except Exception:
        tc90 = {}

    rows = []
    for cert, rec in ctx["by_month"][prev_month].items():
        if cert in payroll_certs or cert in existing_certs:
            continue
        info = tc90.get(cert) or {}
        end_ym = int(info.get("salary_end_ym") or 0)
        hist = _person_history(ctx, cert)
        zero_months = sum(1 for _m, r in hist
                          if abs(r["income"]) <= TOLERANCE and r["insurance"] <= TOLERANCE)
        unit_name = rec["raw_unit"]
        unit_rate = _unit_zero_rate(ctx, unit_name)
        prev_status = "零申报" if abs(rec["income"]) <= TOLERANCE else "有收入"

        if end_ym and end_ym < pay_month:
            suggested, confidence, rule = "skip", "high", "left"
            reason = (f"工资结束年月{end_ym}早于本期{pay_month}，判定已离职，"
                      "倾向不申报并建议减员")
        elif not info:
            suggested, confidence, rule = "skip", "medium", "outside"
            reason = "系统无合同（可能人工管理），倾向不申报"
        elif zero_months >= 1 or prev_status == "零申报":
            suggested, confidence, rule = "declare", "high", "history_zero"
            reason = (f"历史规律：近{len(hist)}月零申报{zero_months}次、"
                      f"上期{prev_status}，倾向零申报保留在册")
        elif unit_rate is not None and unit_rate >= 0.2:
            suggested, confidence, rule = "declare", "medium", "unit_history"
            reason = f"历史规律：单位零申报率{unit_rate:.0%}，倾向零申报保留在册"
        else:
            suggested, confidence, rule = "skip", "medium", "no_payroll"
            reason = "上期有申报但本期无工资记录且无零申报惯性，倾向不申报"

        rows.append({
            "cert_no": cert,
            "name": rec["name"] or (info.get("name") or ""),
            "unit": int(info.get("unit_code") or 0),
            "unit_name": unit_name or (info.get("unit_name") or ""),
            "category": "hist_roster",
            "category_label": CATEGORY_LABELS["hist_roster"],
            "source": "history",
            "salary_months": [],
            "income_total": 0.0,
            "suggested": suggested,
            "default_chosen": suggested,
            "reason": reason,
            "confidence": confidence,
            "prediction_rule": rule,
            "persisted": False,
            "last_filing_month": prev_month,
            "last_income": float(rec["income"]),
            "history_months": len(hist),
            "history_zero_months": zero_months,
            "prev_filing_status": prev_status,
            "prev_filing_income": float(rec["income"]),
            "history_unit": unit_name,
            "unit_zero_rate": round(unit_rate, 4) if unit_rate is not None else None,
            "history_note": (f"上期申报收入{float(rec['income']):.2f}；"
                             f"近{len(hist)}月零申报{zero_months}次"),
            "sys_info": (f"工资结束:{end_ym}" if end_ym else
                         ("系统内无合同" if not info else "")),
        })
    rows.sort(key=lambda r: (
        {"high": 0, "medium": 1, "low": 2}.get(r["confidence"], 3),
        0 if r["default_chosen"] == "declare" else 1,
        r["unit_name"], r["cert_no"]))
    return rows


def _main_unit_rows(conn, records: list, ctx: dict) -> list:
    """多结算单元发薪人员的主结算单元预判（单元级五险一金覆盖度）。"""
    from queries import get_unit_insurance_stats
    from tax_merge import (MAIN_UNIT_MIN_PEOPLE, MAIN_UNIT_INSURED_RATIO,
                           _main_unit_of)

    by_cert = defaultdict(list)
    for r in records:
        cert = str(r.身份证 or r.职工号 or "").strip().upper()
        if cert:
            by_cert[cert].append(r)

    multi = {}
    for cert, recs in by_cert.items():
        if len({int(r.结算单元 or 0) for r in recs}) > 1:
            multi[cert] = recs
    if not multi:
        return []

    units = sorted({int(r.结算单元 or 0) for recs in multi.values() for r in recs})
    months = sorted({int(r.工资所属年月 or 0) for recs in multi.values() for r in recs})
    stats = get_unit_insurance_stats(conn, units, months)
    main_units = {u for u, s in stats.items()
                  if s["people"] >= MAIN_UNIT_MIN_PEOPLE
                  and s["insured"] / s["people"] >= MAIN_UNIT_INSURED_RATIO}

    rows = []
    for cert, recs in multi.items():
        unit_first_month = {}
        unit_names = {}
        for r in recs:
            u = int(r.结算单元 or 0)
            m = int(r.工资所属年月 or 0)
            unit_first_month[u] = min(unit_first_month.get(u, m), m)
            unit_names[u] = str(r.结算单元名称 or "").strip()
        pred = _main_unit_of(recs, main_units)
        pred_name = unit_names.get(pred, "")
        in_main = pred in main_units
        hist = _person_history(ctx, cert)
        hist_units = [rec["raw_unit"] for _m, rec in hist if rec["raw_unit"]]
        hist_unit = hist_units[-1] if hist_units else ""
        hist_match = bool(hist_unit) and _norm_unit(hist_unit) == _norm_unit(pred_name)
        if in_main and (not hist_unit or hist_match):
            confidence = "high"
        elif in_main:
            confidence = "medium"
        else:
            confidence = "low"
        if in_main and hist_match:
            reason = "该单元五险一金覆盖率达标，且与历史申报单位一致"
        elif in_main and hist_unit:
            reason = f"该单元五险一金覆盖率达标；历史申报单位为 {hist_unit}，需人工确认"
        elif in_main:
            reason = "该单元五险一金覆盖率达标，历史无申报单位可比对"
        else:
            reason = "本人所有发薪单元均非主单元，回落最早发薪单元（置信度低）"
        rows.append({
            "cert_no": cert,
            "name": str(recs[0].姓名 or "").strip(),
            "units": sorted(unit_first_month),
            "unit_names": {str(u): n for u, n in unit_names.items()},
            "salary_months": sorted({int(r.工资所属年月 or 0) for r in recs}),
            "predicted_unit": pred,
            "predicted_unit_name": pred_name,
            "unit_in_main_set": in_main,
            "history_unit": hist_unit,
            "history_match": hist_match,
            "confidence": confidence,
            "reason": reason,
            "history_note": (f"近{len(hist)}月申报，最近单位 {hist_unit}"
                             if hist_unit else "无历史申报单位"),
        })
    rows.sort(key=lambda r: (
        {"high": 0, "medium": 1, "low": 2}.get(r["confidence"], 3),
        r["name"], r["cert_no"]))
    return rows


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def build_forecast(pay_month: int, refresh: bool = False) -> dict:
    """生成指定发放月的报税预判。结果缓存 10 分钟。"""
    pay_month = int(pay_month)
    now = time.time()
    cached = _CACHE.get(("forecast", pay_month))
    if cached and not refresh and now - cached[0] < _CACHE_TTL:
        return cached[1]

    from db import get_connection
    from queries import get_suggestions, get_salary_records_by_combos
    from tax_merge import build_merge_suggestions
    from tax_zero import build_zero_salary_suggestions
    from config_db import get_tax_roster

    conn = get_connection()
    try:
        combos = get_suggestions(conn, pay_month)
        if not combos:
            result = {"pay_month": pay_month, "error":
                      f"{pay_month} 无已确认发放批次（TC8M.ATC8M3=2）", "summary": {}}
            _CACHE[("forecast", pay_month)] = (now, result)
            return result
        records = get_salary_records_by_combos(conn, combos)
        ctx = _history_context(pay_month)
        prev_month = ctx.get("prev_month")

        merge_res = build_merge_suggestions(
            conn, pay_month, combos, history_month=prev_month,
            filing_map_provider=_filing_map)
        merge_candidates = [_predict_merge_candidate(c, ctx)
                            for c in (merge_res.get("candidates") or [])]
        merge_candidates.sort(key=lambda c: (
            {"high": 0, "medium": 1, "low": 2}.get(c.get("confidence"), 3),
            c.get("default_chosen") != "double",
            c.get("name") or "", c.get("cert_no") or ""))

        roster = get_tax_roster() or []
        zero_res = build_zero_salary_suggestions(conn, pay_month, combos,
                                                 roster=roster)
        zero_rows = _predict_zero_rows(zero_res, ctx)
        existing = {r["cert_no"] for r in zero_rows} | {c["cert_no"] for c in merge_candidates}
        hist_rows = _history_roster_rows(conn, ctx, pay_month, records, existing)
        main_rows = _main_unit_rows(conn, records, ctx)

        pred_merge = defaultdict(int)
        for c in merge_candidates:
            pred_merge[c.get("default_chosen") or "single"] += 1
        pred_zero = defaultdict(int)
        for r in zero_rows + hist_rows:
            pred_zero[r.get("default_chosen") or "declare"] += 1

        summary = {
            "combos": len(combos),
            "pay_persons": len({str(r.身份证 or r.职工号 or "").strip().upper()
                                for r in records if (r.身份证 or r.职工号)}),
            "prev_filing_month": prev_month,
            "history_months": ctx.get("months") or [],
            "merge_candidates": len(merge_candidates),
            "merge_single": pred_merge.get("single", 0),
            "merge_double": pred_merge.get("double", 0),
            "merge_skip": pred_merge.get("skip", 0),
            "zero_candidates": len(zero_rows),
            "zero_hist_roster": len(hist_rows),
            "zero_declare": pred_zero.get("declare", 0),
            "zero_skip": pred_zero.get("skip", 0),
            "multi_unit_persons": len(main_rows),
            "main_unit_high": sum(1 for r in main_rows if r["confidence"] == "high"),
        }
        result = {
            "pay_month": pay_month,
            "summary": summary,
            "merge_candidates": merge_candidates,
            "zero_candidates": zero_rows,
            "history_roster": hist_rows,
            "main_units": main_rows,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _CACHE[("forecast", pay_month)] = (now, result)
        return result
    finally:
        conn.close()


def get_forecast_months() -> list:
    """发放月列表（Oracle TC8M 已确认发放），附个税端是否已导入。"""
    idx = _filing_index()
    filed = set(idx["months"])
    from db import get_connection
    from queries import get_pay_months
    conn = get_connection()
    try:
        months = get_pay_months(conn)
    finally:
        conn.close()
    return [{"month": m.value, "label": m.label,
             "has_filing": m.value in filed} for m in months]


# ---------------------------------------------------------------------------
# 历史回测
# ---------------------------------------------------------------------------

def _score_merge(merge_res: dict, actual: dict) -> dict:
    stat = {"candidates": 0, "not_filed": 0, "unjudged": 0,
            "comparable": 0, "correct": 0, "accuracy": None,
            "by_prediction": {}, "samples": []}
    for c in merge_res.get("candidates") or []:
        stat["candidates"] += 1
        cert = c["cert_no"]
        rec = actual.get(cert)
        if rec is None:
            stat["not_filed"] += 1
            continue
        actual_ins = rec["insurance"]
        choices = {"single": _dec(c["insurance_single"])}
        double = _dec(c["insurance_double"])
        if double != choices["single"]:
            choices["double"] = double
        if c.get("can_skip"):
            choices["skip"] = Decimal("0")
        matched = [k for k, v in choices.items() if abs(v - actual_ins) <= TOLERANCE]
        if len(matched) != 1:
            stat["unjudged"] += 1
            if len(stat["samples"]) < 10:
                stat["samples"].append({
                    "cert_no": cert, "name": c.get("name"),
                    "predicted": c.get("default_chosen"),
                    "actual_label": "无法判定",
                    "actual_insurance": float(actual_ins),
                    "single": float(choices.get("single", 0)),
                    "double": float(choices.get("double", 0)),
                })
            continue
        actual_label = matched[0]
        predicted = c.get("default_chosen") or c.get("suggested")
        stat["comparable"] += 1
        ok = predicted == actual_label
        stat["correct"] += int(ok)
        key = f"{predicted}->{actual_label}"
        bp = stat["by_prediction"].setdefault(
            predicted, {"predicted": predicted, "count": 0, "correct": 0})
        bp["count"] += 1
        bp["correct"] += int(ok)
        if not ok and len(stat["samples"]) < 10:
            stat["samples"].append({
                "cert_no": cert, "name": c.get("name"),
                "predicted": predicted, "actual_label": actual_label,
                "actual_insurance": float(actual_ins),
                "single": float(choices.get("single", 0)),
                "double": float(choices.get("double", 0)),
            })
    if stat["comparable"]:
        stat["accuracy"] = round(stat["correct"] / stat["comparable"], 4)
    return stat


def _score_zero(rows: list, actual: dict) -> dict:
    stat = {"candidates": 0, "not_comparable": 0,
            "comparable": 0, "correct": 0, "accuracy": None,
            "by_category": {}, "samples": []}
    for r in rows:
        stat["candidates"] += 1
        cert = r["cert_no"]
        rec = actual.get(cert)
        if rec is not None and rec["income"] > TOLERANCE:
            stat["not_comparable"] += 1
            continue
        actual_label = "declare" if rec is not None else "skip"
        predicted = r.get("default_chosen") or r.get("suggested")
        cat = r.get("category") or ""
        bc = stat["by_category"].setdefault(
            cat, {"category": cat, "label": CATEGORY_LABELS.get(cat, cat),
                  "count": 0, "correct": 0})
        stat["comparable"] += 1
        ok = predicted == actual_label
        stat["correct"] += int(ok)
        bc["count"] += 1
        bc["correct"] += int(ok)
        if not ok and len(stat["samples"]) < 10:
            stat["samples"].append({
                "cert_no": cert, "name": r.get("name"),
                "category": cat, "predicted": predicted,
                "actual_label": actual_label,
                "prev_filing_status": r.get("prev_filing_status"),
                "history_note": r.get("history_note"),
            })
    if stat["comparable"]:
        stat["accuracy"] = round(stat["correct"] / stat["comparable"], 4)
    for bc in stat["by_category"].values():
        bc["accuracy"] = round(bc["correct"] / bc["count"], 4) if bc["count"] else None
    return stat


def _score_main_unit(rows: list, actual: dict) -> dict:
    stat = {"candidates": 0, "not_comparable": 0,
            "comparable": 0, "correct": 0, "accuracy": None, "samples": []}
    for r in rows:
        stat["candidates"] += 1
        rec = actual.get(r["cert_no"])
        if rec is None or not rec["unit"]:
            stat["not_comparable"] += 1
            continue
        stat["comparable"] += 1
        ok = _norm_unit(r["predicted_unit_name"]) == rec["unit"]
        stat["correct"] += int(ok)
        if not ok and len(stat["samples"]) < 10:
            stat["samples"].append({
                "cert_no": r["cert_no"], "name": r.get("name"),
                "predicted_unit": r["predicted_unit_name"],
                "actual_unit": rec["raw_unit"],
            })
    if stat["comparable"]:
        stat["accuracy"] = round(stat["correct"] / stat["comparable"], 4)
    return stat


def run_backtest(months=None, refresh: bool = False) -> dict:
    """对过去申报月做滚动回测：用该月之前的历史生成预判，与实际申报对比。"""
    idx = _filing_index()
    all_months = [m for m in idx["months"] if m >= 202512]
    if months:
        targets = [m for m in all_months if m in set(months)]
    else:
        targets = all_months[-6:]
    key = ("backtest", tuple(targets))
    now = time.time()
    cached = _CACHE.get(key)
    if cached and not refresh and now - cached[0] < _CACHE_TTL:
        return cached[1]

    from db import get_connection
    from queries import get_suggestions, get_salary_records_by_combos
    from tax_merge import build_merge_suggestions
    from tax_zero import build_zero_salary_suggestions
    from config_db import get_tax_roster

    results = []
    conn = get_connection()
    try:
        roster = get_tax_roster() or []
        for m in targets:
            ctx = _history_context(m)
            actual = idx["by_month"].get(m, {})
            combos = get_suggestions(conn, m)
            if not combos:
                results.append({"month": m, "error": "无已确认发放批次"})
                continue
            records = get_salary_records_by_combos(conn, combos)
            prev_month = ctx.get("prev_month")
            merge_res = build_merge_suggestions(
                conn, m, combos, history_month=prev_month,
                filing_map_provider=_filing_map)
            merge_rows = [_predict_merge_candidate(c, ctx)
                          for c in (merge_res.get("candidates") or [])]
            zero_res = build_zero_salary_suggestions(conn, m, combos, roster=roster)
            zero_rows = _predict_zero_rows(zero_res, ctx)
            existing = {r["cert_no"] for r in zero_rows}
            hist_rows = _history_roster_rows(conn, ctx, m, records, existing)
            main_rows = _main_unit_rows(conn, records, ctx)

            results.append({
                "month": m,
                "prev_filing_month": prev_month,
                "pay_persons": len({str(r.身份证 or r.职工号 or "").strip().upper()
                                    for r in records if (r.身份证 or r.职工号)}),
                "filing_persons": len(actual),
                "merge": _score_merge({"candidates": merge_rows}, actual),
                "zero": _score_zero(zero_rows, actual),
                "history_roster": _score_zero(hist_rows, actual),
                "main_unit": _score_main_unit(main_rows, actual),
            })
    finally:
        conn.close()

    def _agg(field):
        comp = sum(r[field]["comparable"] for r in results if field in r)
        corr = sum(r[field]["correct"] for r in results if field in r)
        return {"comparable": comp, "correct": corr,
                "accuracy": round(corr / comp, 4) if comp else None}

    overall = {
        "months": [r["month"] for r in results if "merge" in r],
        "merge": _agg("merge"),
        "zero": _agg("zero"),
        "history_roster": _agg("history_roster"),
        "main_unit": _agg("main_unit"),
    }
    result = {"months": results, "overall": overall,
              "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _CACHE[key] = (now, result)
    return result


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

def _write_sheet(ws, headers, rows):
    ws.append(headers)
    for row in rows:
        ws.append(row)
    for cell in ws[1]:
        from openpyxl.styles import Font
        cell.font = Font(bold=True)


def export_excel(pay_month: int) -> bytes:
    """导出预判 Excel：汇总 / 合并预判 / 零申报预判 / 历史在册 / 主单元。"""
    import io
    from openpyxl import Workbook

    data = build_forecast(pay_month)
    if data.get("error"):
        raise ValueError(data["error"])
    s = data["summary"]
    wb = Workbook()

    ws = wb.active
    ws.title = "预判汇总"
    ws.append(["项目", "值"])
    for k, v in [
        ("发放月", data["pay_month"]),
        ("上一申报月", s.get("prev_filing_month")),
        ("历史窗口", ", ".join(str(x) for x in s.get("history_months") or [])),
        ("发放批次数", s.get("combos")),
        ("本期发薪人数", s.get("pay_persons")),
        ("合并候选人数", s.get("merge_candidates")),
        ("合并-单月", s.get("merge_single")),
        ("合并-多月", s.get("merge_double")),
        ("合并-不报", s.get("merge_skip")),
        ("零申报候选(A1/A2/B/C)", s.get("zero_candidates")),
        ("历史在册无工资候选", s.get("zero_hist_roster")),
        ("零申报-申报", s.get("zero_declare")),
        ("零申报-不申报", s.get("zero_skip")),
        ("多结算单元人数", s.get("multi_unit_persons")),
        ("主单元高置信度人数", s.get("main_unit_high")),
        ("生成时间", data.get("generated_at")),
    ]:
        ws.append([k, v])

    ws = wb.create_sheet("合并预判")
    _write_sheet(ws, [
        "证件号码", "姓名", "工号", "主结算单元", "预测", "置信度", "依据",
        "涉及结算单元", "工资所属月", "本期收入", "单月三险", "多月三险",
        "发薪模式", "上期状态", "历史说明", "可选不报", "用户已保存"],
        [[c["cert_no"], c.get("name"), c.get("emp_no"),
          c.get("main_unit_name"), c.get("default_label"), c.get("confidence"),
          c.get("reason"), "；".join(c.get("unit_names", {}).values()),
          ",".join(str(x) for x in c.get("salary_months") or []),
          c.get("income_total"), c.get("insurance_single"),
          c.get("insurance_double"), c.get("pay_pattern"),
          c.get("prev_filing_status"), c.get("history_note"),
          "是" if c.get("can_skip") else "否",
          "是" if c.get("persisted") else "否"] for c in data["merge_candidates"]])

    ws = wb.create_sheet("零申报预判")
    _write_sheet(ws, [
        "证件号码", "姓名", "结算单元", "来源分类", "预测", "置信度", "依据",
        "工资所属月", "收入合计", "上期状态", "历史说明", "单位配置", "用户已保存"],
        [[r["cert_no"], r.get("name"), r.get("unit_name"),
          r.get("category_label"), ZERO_LABELS.get(r.get("default_chosen")),
          r.get("confidence"), r.get("reason"),
          ",".join(str(x) for x in r.get("salary_months") or []),
          r.get("income_total"), r.get("prev_filing_status"),
          r.get("history_note"), r.get("unit_config"),
          "是" if r.get("persisted") else "否"] for r in data["zero_candidates"]])

    ws = wb.create_sheet("历史在册无工资")
    _write_sheet(ws, [
        "证件号码", "姓名", "上期申报单位", "预测", "置信度", "依据",
        "上期申报月", "上期收入", "无效历史说明"],
        [[r["cert_no"], r.get("name"), r.get("unit_name"),
          ZERO_LABELS.get(r.get("default_chosen")), r.get("confidence"),
          r.get("reason"), r.get("last_filing_month"), r.get("last_income"),
          r.get("history_note")] for r in data["history_roster"]])

    ws = wb.create_sheet("主单元预判")
    _write_sheet(ws, [
        "证件号码", "姓名", "预测主单元", "置信度", "依据", "历史申报单位",
        "是否一致", "涉及结算单元", "工资所属月"],
        [[r["cert_no"], r.get("name"), r.get("predicted_unit_name"),
          r.get("confidence"), r.get("reason"), r.get("history_unit"),
          "是" if r.get("history_match") else "否",
          "；".join(r.get("unit_names", {}).values()),
          ",".join(str(x) for x in r.get("salary_months") or [])]
         for r in data["main_units"]])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_backtest_excel(months=None) -> bytes:
    """导出历史回测汇总 Excel。"""
    import io
    from openpyxl import Workbook

    data = run_backtest(months)
    wb = Workbook()
    ws = wb.active
    ws.title = "回测汇总"
    ws.append(["月份", "发薪人数", "申报人数",
               "合并可比", "合并正确", "合并准确率",
               "零申报可比", "零申报正确", "零申报准确率",
               "历史在册可比", "历史在册正确", "历史在册准确率",
               "主单元可比", "主单元正确", "主单元准确率"])
    for r in data["months"]:
        if "merge" not in r:
            ws.append([r.get("month"), "", "", "", "", r.get("error", "")])
            continue
        ws.append([
            r["month"], r.get("pay_persons"), r.get("filing_persons"),
            r["merge"]["comparable"], r["merge"]["correct"], r["merge"]["accuracy"],
            r["zero"]["comparable"], r["zero"]["correct"], r["zero"]["accuracy"],
            r["history_roster"]["comparable"], r["history_roster"]["correct"],
            r["history_roster"]["accuracy"],
            r["main_unit"]["comparable"], r["main_unit"]["correct"],
            r["main_unit"]["accuracy"],
        ])
    o = data["overall"]
    ws.append([])
    ws.append(["合计",
               "", "",
               o["merge"]["comparable"], o["merge"]["correct"], o["merge"]["accuracy"],
               o["zero"]["comparable"], o["zero"]["correct"], o["zero"]["accuracy"],
               o["history_roster"]["comparable"], o["history_roster"]["correct"],
               o["history_roster"]["accuracy"],
               o["main_unit"]["comparable"], o["main_unit"]["correct"],
               o["main_unit"]["accuracy"]])

    for key, title in (("merge", "合并误判样例"), ("zero", "零申报误判样例"),
                       ("history_roster", "历史在册误判样例"),
                       ("main_unit", "主单元误判样例")):
        ws = wb.create_sheet(title)
        ws.append(["月份", "证件号码", "姓名", "预测", "实际", "说明/金额"])
        for r in data["months"]:
            if key not in r:
                continue
            for sample in r[key].get("samples") or []:
                if key == "merge":
                    detail = (f"实际三险{sample.get('actual_insurance')}；"
                              f"单月{sample.get('single')}；多月{sample.get('double')}")
                elif key == "main_unit":
                    detail = f"预测{sample.get('predicted_unit')}；实际{sample.get('actual_unit')}"
                else:
                    detail = sample.get("history_note") or ""
                ws.append([r["month"], sample.get("cert_no"), sample.get("name"),
                           sample.get("predicted") or sample.get("predicted_unit"),
                           sample.get("actual_label") or sample.get("actual_unit"),
                           detail])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
