"""tax_compare.py - 生成报税数据 vs 个税端税款计算 逐人逐月核对

口径
----
- 月份: 个税端「税款所属期起」= 发放月 (TC8M.ATC8G7), 已用跨月发放样例验证:
  安刚 202608 发放 202607(1140)+202608(3370), 个税端 202608 收入=4510 精确一致。
  注意: config_db.social_security_history.salary_month 字段沿用旧名, 实为申报/发放月。
- 系统侧: Oracle TC8M 该发放月已确认发放批次的组合 (ATC8M3=2), 取 TC93 记录后
  按证件号码聚合 (跨工资所属月/多批次/多结算单元求和);
  本期收入用 calc_本期收入(), 五险一金取个人缴 BAA001/BAA002/BAA003/CAA002,
  免税取 ATC936, 个税取 ATC93D。
- 个税端: config_db.social_security_history (所得项目=正常工资薪金), 一人一月一行。
- 容差: 0.01 元。

状态
----
仅系统(有收入) / 仅个税端(有收入) / 姓名不一致 / 收入差异 /
五险一金差异 / 个税差异 / 单位差异 / 仅系统(零收入) / 仅个税端(零收入) / 一致
"""
import time
from decimal import Decimal

STATUS_ORDER = {
    "仅系统(有收入)": 0,
    "仅个税端(有收入)": 1,
    "姓名不一致": 2,
    "收入差异": 3,
    "五险一金差异": 4,
    "个税差异": 5,
    "单位差异": 6,
    "仅系统(零收入)": 7,
    "仅个税端(零收入)": 8,
    "一致": 9,
}

TOLERANCE = Decimal("0.01")
INSURANCE_FIELDS = [
    ("pension", "养老"),
    ("medical", "医疗"),
    ("unemployment", "失业"),
    ("housing", "公积金"),
]

_CACHE = {}
_CACHE_TTL = 300  # 秒


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")


def _fmt(v: Decimal) -> str:
    return f"{float(v):.2f}"


def _differs(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) > TOLERANCE


def _norm_unit(name: str) -> str:
    """单位名称归一化: 去掉末尾 B/批次后缀与括号说明, 避免同单位多编码误报。"""
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


def get_available_months() -> list:
    """个税端已导入的月份列表 (含条数), 用于页面下拉。"""
    from config_db import get_db
    conn = get_db()
    rows = conn.execute(
        "SELECT salary_month, COUNT(*) AS n FROM social_security_history "
        "GROUP BY salary_month ORDER BY salary_month DESC").fetchall()
    conn.close()
    return [{"month": int(r[0]), "count": int(r[1])} for r in rows]


def _build_generated_side(month: int) -> dict:
    """Oracle TC8M 该发放月已发批次 → TC93 记录 → 按证件号码聚合的系统生成侧数据。

    关键: 个税端税款所属期 = 发放月, 与工资所属年月(ATC931)不同; 跨月发放
    (如 202608 发 202607+202608 工资) 必须按发放月组合取数后合并, 否则会
    把同一人的收入拆到两个系统月份, 与个税端对不上。
    """
    from db import get_connection
    from queries import get_suggestions, get_salary_records_by_combos
    from templates_gen.formulas import calc_本期收入

    conn = get_connection()
    try:
        combos = get_suggestions(conn, month)
        records = get_salary_records_by_combos(conn, combos) if combos else []
    finally:
        conn.close()

    gen = {}
    for r in records:
        cert = (r.身份证 or "").strip().upper()
        if not cert:
            continue
        d = gen.get(cert)
        if d is None:
            d = gen[cert] = {
                "cert_no": cert,
                "name": (r.姓名 or "").strip(),
                "income": Decimal("0"),
                "tax_free": Decimal("0"),
                "pension": Decimal("0"),
                "medical": Decimal("0"),
                "unemployment": Decimal("0"),
                "housing": Decimal("0"),
                "tax": Decimal("0"),
                "units": [],
                "rows": 0,
            }
        d["income"] += calc_本期收入(r)
        d["tax_free"] += _dec(r.补发3)
        d["pension"] += _dec(r.养老个人)
        d["medical"] += _dec(r.医疗个人)
        d["unemployment"] += _dec(r.失业个人)
        d["housing"] += _dec(r.公积金个人)
        d["tax"] += _dec(r.个人所得税)
        d["rows"] += 1
        unit = (r.结算单元名称 or "").strip()
        if unit and unit not in d["units"]:
            d["units"].append(unit)
    return gen


def _build_filing_side(month: int) -> dict:
    """个税端税款计算数据 (config_db) → 按证件号码索引。"""
    from config_db import get_db
    conn = get_db()
    rows = conn.execute(
        '''SELECT 证件号码, 姓名, 所得项目, 本期收入, 本期免税收入,
                  本期基本养老保险费, 本期基本医疗保险费, 本期失业保险费,
                  本期住房公积金, "应补(退)税额", 备注
           FROM social_security_history WHERE salary_month=?''',
        (int(month),)).fetchall()
    conn.close()
    fil = {}
    for r in rows:
        item = (r[2] or "").strip()
        if item and item != "正常工资薪金":
            continue
        cert = (r[0] or "").strip().upper()
        if not cert:
            continue
        fil[cert] = {
            "cert_no": cert,
            "name": (r[1] or "").strip(),
            "income": _dec(r[3]),
            "tax_free": _dec(r[4]),
            "pension": _dec(r[5]),
            "medical": _dec(r[6]),
            "unemployment": _dec(r[7]),
            "housing": _dec(r[8]),
            "tax": _dec(r[9]),
            "unit": (r[10] or "").strip(),
        }
    return fil


def _empty_side() -> dict:
    return {
        "cert_no": "", "name": "", "income": Decimal("0"), "tax_free": Decimal("0"),
        "pension": Decimal("0"), "medical": Decimal("0"), "unemployment": Decimal("0"),
        "housing": Decimal("0"), "tax": Decimal("0"),
    }


def compare_month(month: int, refresh: bool = False) -> dict:
    """逐人核对指定月份, 返回 {summary, details}。结果内存缓存 5 分钟。"""
    now = time.time()
    cached = _CACHE.get(month)
    if cached and not refresh and now - cached[0] < _CACHE_TTL:
        return cached[1]

    gen = _build_generated_side(month)
    fil = _build_filing_side(month)

    details = []
    for cert in sorted(set(gen) | set(fil)):
        g = gen.get(cert)
        f = fil.get(cert)
        gd = g or _empty_side()
        fd = f or _empty_side()
        g_income, f_income = gd["income"], fd["income"]
        income_diff = f_income - g_income
        g_insurance = gd["pension"] + gd["medical"] + gd["unemployment"] + gd["housing"]
        f_insurance = fd["pension"] + fd["medical"] + fd["unemployment"] + fd["housing"]
        insurance_diff = f_insurance - g_insurance
        tax_diff = fd["tax"] - gd["tax"]
        tax_free_diff = fd["tax_free"] - gd["tax_free"]
        g_units = "；".join(gd.get("units") or [])
        f_unit = fd.get("unit") or ""

        status = ""
        note = ""
        if f is None:
            if g_income > TOLERANCE:
                status = "仅系统(有收入)"
                note = f"个税端无此人；系统 {_fmt(g_income)} 元"
            else:
                status = "仅系统(零收入)"
                note = "个税端无此人；系统本期收入为 0（零申报未上报）"
        elif g is None:
            if f_income > TOLERANCE:
                status = "仅个税端(有收入)"
                note = f"系统无此人；个税端 {_fmt(f_income)} 元 / {f_unit or '无单位'}"
            else:
                status = "仅个税端(零收入)"
                note = f"系统无此人；个税端零申报 / {f_unit or '无单位'}"
        elif gd["name"] and fd["name"] and gd["name"] != fd["name"]:
            status = "姓名不一致"
            note = f"系统 {gd['name']} vs 个税端 {fd['name']}"
        elif _differs(g_income, f_income):
            status = "收入差异"
            note = f"系统 {_fmt(g_income)} vs 个税端 {_fmt(f_income)}（差 {_fmt(income_diff)}）"
        else:
            parts = []
            for key, label in INSURANCE_FIELDS:
                if _differs(gd[key], fd[key]):
                    parts.append(f"{label} 系统 {_fmt(gd[key])} / 个税端 {_fmt(fd[key])}")
            if parts:
                status = "五险一金差异"
                note = "；".join(parts)
            elif _differs(gd["tax"], fd["tax"]):
                status = "个税差异"
                note = f"系统 {_fmt(gd['tax'])} vs 个税端 {_fmt(fd['tax'])}（差 {_fmt(tax_diff)}）"
            elif (_norm_unit(g_units) and _norm_unit(f_unit)
                  and _norm_unit(g_units) != _norm_unit(f_unit)):
                status = "单位差异"
                note = f"系统 {g_units or '无单位'} vs 个税端 {f_unit or '无单位'}"
            else:
                status = "一致"

        details.append({
            "cert_no": cert,
            "gen_name": gd["name"],
            "fil_name": fd["name"],
            "gen_unit": g_units,
            "fil_unit": f_unit,
            "gen_income": round(float(g_income), 2),
            "fil_income": round(float(f_income), 2),
            "income_diff": round(float(income_diff), 2),
            "gen_tax_free": round(float(gd["tax_free"]), 2),
            "fil_tax_free": round(float(fd["tax_free"]), 2),
            "tax_free_diff": round(float(tax_free_diff), 2),
            "gen_pension": round(float(gd["pension"]), 2),
            "fil_pension": round(float(fd["pension"]), 2),
            "pension_diff": round(float(fd["pension"] - gd["pension"]), 2),
            "gen_medical": round(float(gd["medical"]), 2),
            "fil_medical": round(float(fd["medical"]), 2),
            "medical_diff": round(float(fd["medical"] - gd["medical"]), 2),
            "gen_unemployment": round(float(gd["unemployment"]), 2),
            "fil_unemployment": round(float(fd["unemployment"]), 2),
            "unemployment_diff": round(float(fd["unemployment"] - gd["unemployment"]), 2),
            "gen_housing": round(float(gd["housing"]), 2),
            "fil_housing": round(float(fd["housing"]), 2),
            "housing_diff": round(float(fd["housing"] - gd["housing"]), 2),
            "gen_insurance": round(float(g_insurance), 2),
            "fil_insurance": round(float(f_insurance), 2),
            "insurance_diff": round(float(insurance_diff), 2),
            "gen_tax": round(float(gd["tax"]), 2),
            "fil_tax": round(float(fd["tax"]), 2),
            "tax_diff": round(float(tax_diff), 2),
            "gen_rows": gd.get("rows", 0),
            "status": status,
            "note": note,
        })

    details.sort(key=lambda d: (
        STATUS_ORDER.get(d["status"], 99),
        -abs(d["income_diff"]),
        d["gen_name"] or d["fil_name"],
        d["cert_no"],
    ))

    status_counts = {}
    for d in details:
        status_counts[d["status"]] = status_counts.get(d["status"], 0) + 1

    gen_total = sum((d["gen_income"] for d in details), 0.0)
    fil_total = sum((d["fil_income"] for d in details), 0.0)
    gen_ins_total = sum((d["gen_insurance"] for d in details), 0.0)
    fil_ins_total = sum((d["fil_insurance"] for d in details), 0.0)
    gen_tax_total = sum((d["gen_tax"] for d in details), 0.0)
    fil_tax_total = sum((d["fil_tax"] for d in details), 0.0)

    summary = {
        "month": month,
        "gen_persons": len(gen),
        "fil_persons": len(fil),
        "common_persons": len(set(gen) & set(fil)),
        "only_gen": len(set(gen) - set(fil)),
        "only_fil": len(set(fil) - set(gen)),
        "matched": status_counts.get("一致", 0),
        "diff": sum(v for k, v in status_counts.items()
                    if k not in ("一致", "仅系统(零收入)", "仅个税端(零收入)")),
        "status_counts": status_counts,
        "gen_income": round(gen_total, 2),
        "fil_income": round(fil_total, 2),
        "income_diff": round(fil_total - gen_total, 2),
        "gen_insurance": round(gen_ins_total, 2),
        "fil_insurance": round(fil_ins_total, 2),
        "insurance_diff": round(fil_ins_total - gen_ins_total, 2),
        "gen_tax": round(gen_tax_total, 2),
        "fil_tax": round(fil_tax_total, 2),
        "tax_diff": round(fil_tax_total - gen_tax_total, 2),
    }
    result = {"summary": summary, "details": details}
    _CACHE[month] = (now, result)
    return result


def query_details(month: int, status: str = "", search: str = "",
                  page: int = 1, page_size: int = 100,
                  refresh: bool = False) -> dict:
    """分页查询核对明细 (供 API 使用)。"""
    result = compare_month(month, refresh=refresh)
    details = result["details"]
    if status:
        details = [d for d in details if d["status"] == status]
    if search:
        kw = search.strip().upper()
        details = [d for d in details
                   if kw in d["cert_no"].upper()
                   or kw in (d["gen_name"] or "").upper()
                   or kw in (d["fil_name"] or "").upper()]
    total = len(details)
    page_size = max(1, min(int(page_size or 100), 1000))
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(int(page or 1), pages))
    start = (page - 1) * page_size
    return {
        "summary": result["summary"],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "rows": details[start:start + page_size],
    }


def export_excel(month: int) -> bytes:
    """导出核对结果 Excel (汇总 + 明细), 返回文件字节。"""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    result = compare_month(month, refresh=True)
    summary = result["summary"]
    details = result["details"]

    wb = Workbook()
    ws = wb.active
    ws.title = "对账汇总"
    ws.append([f"{month} 期 生成报税数据 vs 个税端税款计算 对账汇总"])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=4)
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    rows = [
        ("系统生成人数", summary["gen_persons"]),
        ("个税端人数", summary["fil_persons"]),
        ("两方均有", summary["common_persons"]),
        ("仅系统侧", summary["only_gen"]),
        ("仅个税端", summary["only_fil"]),
        ("完全一致", summary["matched"]),
        ("存在差异(不含零收入单边)", summary["diff"]),
        ("系统本期收入合计", summary["gen_income"]),
        ("个税端本期收入合计", summary["fil_income"]),
        ("收入差额(个税端-系统)", summary["income_diff"]),
        ("系统五险一金合计", summary["gen_insurance"]),
        ("个税端五险一金合计", summary["fil_insurance"]),
        ("五险一金差额", summary["insurance_diff"]),
        ("系统本期个税合计", summary["gen_tax"]),
        ("个税端应补退税额合计", summary["fil_tax"]),
        ("个税差额", summary["tax_diff"]),
    ]
    for k, v in rows:
        ws.append([k, v])
    ws.append([])
    ws.append(["状态分布", "人数"])
    for st in STATUS_ORDER:
        if st in summary["status_counts"]:
            ws.append([st, summary["status_counts"][st]])
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 18

    ws2 = wb.create_sheet("逐人明细")
    headers = [
        "状态", "证件号码", "系统姓名", "个税端姓名",
        "系统收入", "个税端收入", "收入差",
        "系统养老", "个税端养老", "养老差",
        "系统医疗", "个税端医疗", "医疗差",
        "系统失业", "个税端失业", "失业差",
        "系统公积金", "个税端公积金", "公积金差",
        "系统五险一金合计", "个税端五险一金合计", "五险一金差",
        "系统免税", "个税端免税", "免税差",
        "系统个税", "个税端应补退", "个税差",
        "系统单位", "个税端备注", "系统记录数", "说明",
    ]
    ws2.append(headers)
    for c in ws2[1]:
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal="center")
    for d in details:
        ws2.append([
            d["status"], d["cert_no"], d["gen_name"], d["fil_name"],
            d["gen_income"], d["fil_income"], d["income_diff"],
            d["gen_pension"], d["fil_pension"], d["pension_diff"],
            d["gen_medical"], d["fil_medical"], d["medical_diff"],
            d["gen_unemployment"], d["fil_unemployment"], d["unemployment_diff"],
            d["gen_housing"], d["fil_housing"], d["housing_diff"],
            d["gen_insurance"], d["fil_insurance"], d["insurance_diff"],
            d["gen_tax_free"], d["fil_tax_free"], d["tax_free_diff"],
            d["gen_tax"], d["fil_tax"], d["tax_diff"],
            d["gen_unit"], d["fil_unit"], d["gen_rows"], d["note"],
        ])
    widths = [14, 21, 10, 10] + [11] * 24 + [22, 22, 9, 40]
    for i, w in enumerate(widths, 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "A2"

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
