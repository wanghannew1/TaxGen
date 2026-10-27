"""filing_compare.py - 报税后核对：生成的报税文件 vs 个税端导出文件

用途
----
生成报税文件并导入个税端后，把「本项目生成的模板」与「个税端导出的数据」
按人逐条比对，提前发现：
- 收入/免税/三险一金差异
- 单位(备注)差异
- 仅生成侧（个税端没有 = 漏报/未导入）
- 仅个税端（生成侧没有 = 多报/历史残留）
- 生成侧重复证件号（同一人跨业务员/跨单位多行）
- 同名不同证（同单位同名但身份证号不同，疑似挂错 ID）

数据存储
--------
沿用 tax_return.db（SQLite），新增 generated_record 表；不写 Oracle。
个税端导出侧复用 filing_history.filing_record（税款计算/正常工资薪金/
劳务报酬/全年一次性奖金 4 类）。

月份口径
--------
生成文件本身没有「所得期间起」列，月份必须由页面选择「税款所属期/发放月份」，
不能用文件名里的工资所属月代替（例如 202608 工资在 202609 属期申报）。
"""
import csv
import json
import os
import re
import sqlite3
from datetime import datetime

from tax_return import DB_PATH

TYPE_正常工资薪金 = "正常工资薪金"
TYPE_劳务报酬 = "劳务报酬"
TYPE_全年一次性奖金 = "全年一次性奖金"

GENERATED_TYPES = (TYPE_正常工资薪金, TYPE_劳务报酬, TYPE_全年一次性奖金)

# 生成文件字段映射：目标字段 -> 模板表头（已去掉前导 *）
_FIELD_MAP = {
    TYPE_正常工资薪金: {
        "emp_no": "工号",
        "name": "姓名",
        "id_type": "证件类型",
        "cert_no": "证件号码",
        "income": "本期收入",
        "tax_free": "本期免税收入",
        "pension": "基本养老保险费",
        "medical": "基本医疗保险费",
        "unemployment": "失业保险费",
        "housing": "住房公积金",
        "tax_due": None,
        "remark": "备注",
    },
    TYPE_劳务报酬: {
        "emp_no": "工号",
        "name": "姓名",
        "id_type": "证件类型",
        "cert_no": "证件号码",
        "income": "收入",
        "tax_free": "免税收入",
        "pension": None,
        "medical": None,
        "unemployment": None,
        "housing": None,
        "tax_due": None,
        "remark": "备注",
    },
    TYPE_全年一次性奖金: {
        "emp_no": "工号",
        "name": "姓名",
        "id_type": "证件类型",
        "cert_no": "证件号码",
        "income": "全年一次性奖金额",
        "tax_free": "免税收入",
        "pension": None,
        "medical": None,
        "unemployment": None,
        "housing": None,
        "tax_due": None,
        "remark": "备注",
    },
}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """创建 generated_record 表（允许同一证件多行，用于发现生成侧重复）。"""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS generated_record (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cert_no TEXT NOT NULL,
            name TEXT NOT NULL,
            emp_no TEXT DEFAULT '',
            id_type TEXT DEFAULT '',
            month INTEGER NOT NULL,
            item_type TEXT NOT NULL,
            income REAL DEFAULT 0,
            tax_free REAL DEFAULT 0,
            pension REAL DEFAULT 0,
            medical REAL DEFAULT 0,
            unemployment REAL DEFAULT 0,
            housing REAL DEFAULT 0,
            insurance REAL DEFAULT 0,
            tax_due REAL DEFAULT 0,
            remark TEXT DEFAULT '',
            raw TEXT DEFAULT '{}',
            source_file TEXT DEFAULT '',
            import_time TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_generated_month_type "
                 "ON generated_record(month, item_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_generated_cert "
                 "ON generated_record(cert_no)")
    conn.commit()
    conn.close()


def _as_float(v):
    try:
        return float(str(v or 0).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def _as_str(v):
    if v is None:
        return ""
    return str(v).strip()


def _cert_str(v):
    """证件号转字符串，避免 Excel 数值/科学计数法破坏前导零。"""
    if v is None:
        return ""
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else str(v)
    s = str(v).strip()
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    return s


def _norm_header(h):
    s = _as_str(h)
    while s.startswith("*"):
        s = s[1:].strip()
    return s


def _find_header(rows):
    for i, row in enumerate(rows):
        vals = [_norm_header(c) for c in row]
        if "证件号码" in vals and "姓名" in vals:
            return i, vals
    raise ValueError("文件缺少 姓名/证件号码 表头，无法识别")


def _read_sheet(filepath):
    """读取首个工作表/CSV，返回 (header, data_rows)；表头允许上方有标题行。"""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".csv":
        with open(filepath, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    elif ext == ".xls":
        import xlrd
        wb = xlrd.open_workbook(filepath)
        sh = wb.sheet_by_index(0)
        rows = [[sh.cell_value(r, c) for c in range(sh.ncols)]
                for r in range(sh.nrows)]
    elif ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(filepath, read_only=True, data_only=True)
        ws = wb.active
        rows = [list(row) for row in ws.iter_rows(values_only=True)]
        wb.close()
    else:
        raise ValueError(f"不支持的文件格式: {ext}")
    idx, header = _find_header(rows)
    return header, rows[idx + 1:]


def _detect_type(header):
    """按表头识别生成文件类型；个税端导出文件要拒绝并提示去上方导入。"""
    hset = set(header)
    is_filing_export = ("所得期间起" in hset) or ("税款所属期起" in hset)
    if "全年一次性奖金额" in hset:
        if is_filing_export:
            raise ValueError("这是个税端导出的申报文件，请用上方「申报文件导入」")
        return TYPE_全年一次性奖金
    if "本期收入" in hset and ("基本养老保险费" in hset or "本期基本养老保险费" in hset):
        if is_filing_export:
            raise ValueError("这是个税端导出的申报文件，请用上方「申报文件导入」")
        return TYPE_正常工资薪金
    if "收入" in hset and ("允许扣除的税费" in hset or "费用" in hset):
        if is_filing_export:
            raise ValueError("这是个税端导出的申报文件，请用上方「申报文件导入」")
        return TYPE_劳务报酬
    raise ValueError("无法识别生成文件类型（正常工资薪金/劳务报酬/全年一次性奖金）")


def _parse_month(value):
    """从期间起解析 YYYYMM，兼容 2026-09-01 / 20260901 / xlrd 日期。"""
    if value is None:
        return 0
    if isinstance(value, float):
        try:
            import xlrd
            dt = xlrd.xldate_as_datetime(value, 0)
            return dt.year * 100 + dt.month
        except Exception:
            return 0
    s = str(value).strip()
    m = re.search(r"(\d{4})[-/]?(\d{1,2})", s)
    if m:
        return int(m.group(1)) * 100 + int(m.group(2))
    digits = re.sub(r"\D", "", s)
    return int(digits[:6]) if len(digits) >= 6 else 0


def parse_generated_file(filepath, month, source_file=""):
    """解析单个生成文件，返回规范化记录列表（保留重复证件行）。

    month: 税款所属期/发放月份（YYYYMM），由页面选择。
    """
    if not month or month <= 0:
        raise ValueError("请选择税款所属期/发放月份（生成文件本身没有月份列）")
    header, rows = _read_sheet(filepath)
    item_type = _detect_type(header)
    field_map = _FIELD_MAP[item_type]
    col = {}
    for target, src in field_map.items():
        col[target] = header.index(src) if src and src in header else None
    records = []
    for row in rows:
        def cell(target):
            i = col.get(target)
            return row[i] if i is not None and i < len(row) else None

        cert = _cert_str(cell("cert_no"))
        if not cert:
            continue
        name = _as_str(cell("name"))
        income = _as_float(cell("income"))
        tax_free = _as_float(cell("tax_free"))
        pension = _as_float(cell("pension"))
        medical = _as_float(cell("medical"))
        unemployment = _as_float(cell("unemployment"))
        housing = _as_float(cell("housing"))
        insurance = pension + medical + unemployment + housing
        raw = {}
        for c, h in enumerate(header):
            raw[h] = row[c] if c < len(row) else None
        records.append({
            "cert_no": cert,
            "name": name,
            "emp_no": _as_str(cell("emp_no")),
            "id_type": _as_str(cell("id_type")),
            "month": int(month),
            "item_type": item_type,
            "income": income,
            "tax_free": tax_free,
            "pension": pension,
            "medical": medical,
            "unemployment": unemployment,
            "housing": housing,
            "insurance": insurance,
            "tax_due": _as_float(cell("tax_due")),
            "remark": _as_str(cell("remark")),
            "raw": json.dumps(raw, ensure_ascii=False, default=str),
            "source_file": source_file or os.path.basename(filepath),
        })
    if not records:
        raise ValueError(f"未解析到有效记录: {os.path.basename(filepath)}")
    return records


def import_generated_records(records):
    """按 (月份, 类型) 覆盖式写入 generated_record，返回写入条数。

    同一批可含多个业务员文件；重复证件号保留多行，供比对时标记「生成侧重复」。
    """
    if not records:
        return 0
    groups = {}
    for r in records:
        groups.setdefault((int(r["month"]), str(r["item_type"])), []).append(r)
    init_db()
    conn = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    count = 0
    try:
        for (month, item_type), recs in groups.items():
            conn.execute("DELETE FROM generated_record WHERE month=? AND item_type=?",
                         (month, item_type))
            for r in recs:
                conn.execute("""
                    INSERT INTO generated_record
                    (cert_no, name, emp_no, id_type, month, item_type,
                     income, tax_free, pension, medical, unemployment, housing,
                     insurance, tax_due, remark, raw, source_file, import_time)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (r["cert_no"], r["name"], r["emp_no"], r["id_type"], month, item_type,
                      r["income"], r["tax_free"], r["pension"], r["medical"],
                      r["unemployment"], r["housing"], r["insurance"], r["tax_due"],
                      r["remark"], r["raw"], r["source_file"], now))
                count += 1
        conn.commit()
    finally:
        conn.close()
    return count


def get_generated_types(month):
    init_db()
    conn = get_db()
    rows = conn.execute("""
        SELECT item_type, COUNT(*) AS count
        FROM generated_record WHERE month=?
        GROUP BY item_type ORDER BY item_type
    """, (month,)).fetchall()
    conn.close()
    return [{"item_type": r["item_type"], "count": r["count"]} for r in rows]


def get_generated_rows(month, item_type):
    init_db()
    conn = get_db()
    rows = conn.execute("""
        SELECT cert_no, name, emp_no, id_type, month, item_type,
               income, tax_free, pension, medical, unemployment, housing,
               insurance, tax_due, remark, source_file, import_time
        FROM generated_record
        WHERE month=? AND item_type=?
        ORDER BY cert_no, id
    """, (month, item_type)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _unit(v):
    return _as_str(v)


def _units_of(rows):
    return "；".join(dict.fromkeys(_unit(r["remark"]) for r in rows if _unit(r["remark"])))


def _row_brief(r):
    return f"{_unit(r['remark']) or '无单位'} / {r['income']:.2f}"


def _same_name_groups(gen_by_cert, fil_map):
    """同单位同名不同证 → 疑似挂错身份证。

    只在「生成侧某证件在单位 U 出现，但该证件在个税端并不属于 U；同时个税端
    在 U 另有一个同名不同证的人」时告警。避免把大量合法的跨单位同名员工
    （李爽 15 人、刘畅 30 人…）全部列成告警。
    """
    fil_by_name = {}
    for cert, r in fil_map.items():
        name = _as_str(r.get("name"))
        if name:
            fil_by_name.setdefault(name, []).append(r)

    result = []
    seen = set()
    for cert, grows in gen_by_cert.items():
        if not grows:
            continue
        name = _as_str(grows[0]["name"])
        if not name:
            continue
        f_same = fil_by_name.get(name, [])
        f_units_cert = {_unit(r.get("remark")) for r in f_same
                        if r["cert_no"] == cert and _unit(r.get("remark"))}
        gen_units = {_unit(r["remark"]) for r in grows if _unit(r["remark"])}
        for u in gen_units:
            if u in f_units_cert:
                continue
            others = [r for r in f_same
                      if r["cert_no"] != cert and _unit(r.get("remark")) == u]
            if not others:
                continue
            key = (name, cert, u)
            if key in seen:
                continue
            seen.add(key)
            result.append({
                "name": name,
                "type": "同单位同名不同证（疑似挂错ID）",
                "gen_certs": cert,
                "fil_certs": "、".join(sorted({r["cert_no"] for r in others})),
                "gen_detail": "；".join(f"{r['cert_no']} / {_row_brief(r)}" for r in grows),
                "fil_detail": "；".join(
                    f"{r['cert_no']} / {r.get('remark','') or '无单位'} / {r.get('income', 0):.2f}"
                    for r in others),
                "same_units": u,
            })
    result.sort(key=lambda x: (x["name"], x["same_units"]))
    return result


def compare_generated_vs_filing(month, gen_item_type=TYPE_正常工资薪金,
                                filing_item_type="税款计算"):
    """生成文件 vs 个税端导出，返回 summary/details/same_name。"""
    from filing_history import get_filing_map
    gen_rows = get_generated_rows(month, gen_item_type)
    fil_map = get_filing_map(month, filing_item_type)
    gen_by_cert = {}
    for r in gen_rows:
        gen_by_cert.setdefault(r["cert_no"], []).append(r)

    details = []
    for cert in sorted(set(gen_by_cert) | set(fil_map)):
        grows = gen_by_cert.get(cert, [])
        f = fil_map.get(cert)
        gen_name = _as_str(grows[0]["name"]) if grows else ""
        fil_name = _as_str(f.get("name")) if f else ""
        gen_income = sum(float(r["income"] or 0) for r in grows)
        gen_insurance = sum(float(r["insurance"] or 0) for r in grows)
        gen_unit = _units_of(grows)
        fil_income = float(f.get("income") or 0) if f else None
        fil_insurance = float(f.get("insurance") or 0) if f else None
        income_diff = round(fil_income - gen_income, 2) if f else None
        insurance_diff = round(fil_insurance - gen_insurance, 2) if f else None

        same_name_flag = False
        if not grows:
            status = "仅个税端"
            note = f"生成侧无此人；个税端 {f.get('remark','') or '无单位'} / {fil_income:.2f}"
            others = [r for r in gen_rows
                      if r["cert_no"] != cert and _as_str(r["name"]) == fil_name]
            if others:
                same_name_flag = True
                note += "；【同名不同证】生成侧有：" + "；".join(
                    f"{r['cert_no']} / {_row_brief(r)}" for r in others)
        elif len(grows) > 1:
            status = "生成侧重复"
            note = (f"生成侧 {len(grows)} 行：" + "；".join(_row_brief(r) for r in grows)
                    + (f"；个税端 {f.get('remark','') or '无单位'} / {fil_income:.2f}" if f else "；个税端无此人"))
        elif not f:
            status = "仅生成"
            note = f"个税端无此人；生成侧 {gen_unit or '无单位'} / {gen_income:.2f}"
            others = [r for r in fil_map.values()
                      if r["cert_no"] != cert and _as_str(r.get("name")) == gen_name]
            if others:
                same_name_flag = True
                note += "；【同名不同证】个税端有：" + "；".join(
                    f"{r['cert_no']} / {r.get('remark','') or '无单位'} / {r.get('income', 0):.2f}"
                    for r in others)
        elif gen_name != fil_name:
            status = "姓名不一致"
            note = f"生成 {gen_name} vs 个税端 {fil_name}"
        elif abs(income_diff) > 0.01:
            status = "收入差异"
            note = f"生成 {gen_income:.2f} vs 个税端 {fil_income:.2f}"
        elif abs(insurance_diff) > 0.01:
            status = "三险差异"
            note = f"生成 {gen_insurance:.2f} vs 个税端 {fil_insurance:.2f}"
        elif _unit(gen_unit) != _unit(f.get("remark")):
            status = "单位差异"
            note = f"生成 {gen_unit or '无单位'} vs 个税端 {f.get('remark','') or '无单位'}"
        else:
            status = "一致"
            note = ""

        details.append({
            "cert_no": cert,
            "gen_name": gen_name,
            "fil_name": fil_name,
            "gen_unit": gen_unit,
            "fil_unit": _as_str(f.get("remark")) if f else "",
            "gen_income": round(gen_income, 2),
            "fil_income": round(fil_income, 2) if f else None,
            "income_diff": income_diff,
            "gen_tax_free": round(sum(float(r["tax_free"] or 0) for r in grows), 2),
            "fil_tax_free": round(float(f.get("tax_free") or 0), 2) if f else None,
            "gen_insurance": round(gen_insurance, 2),
            "fil_insurance": round(fil_insurance, 2) if f else None,
            "insurance_diff": insurance_diff,
            "gen_tax_due": round(sum(float(r["tax_due"] or 0) for r in grows), 2),
            "fil_tax_due": round(float(f.get("tax_due") or 0), 2) if f else None,
            "gen_rows": [{"unit": _unit(r["remark"]), "income": round(float(r["income"] or 0), 2),
                          "insurance": round(float(r["insurance"] or 0), 2),
                          "source_file": r.get("source_file", "")} for r in grows],
            "same_name_flag": same_name_flag,
            "status": status,
            "note": note,
        })

    order = {"生成侧重复": 0, "仅生成": 1, "仅个税端": 2, "姓名不一致": 3,
             "收入差异": 4, "三险差异": 5, "单位差异": 6, "一致": 7}
    details.sort(key=lambda d: (order.get(d["status"], 9), d["gen_name"] or d["fil_name"], d["cert_no"]))

    same_name = _same_name_groups(gen_by_cert, fil_map)
    summary = {
        "month": month,
        "gen_item_type": gen_item_type,
        "filing_item_type": filing_item_type,
        "gen_rows": len(gen_rows),
        "gen_certs": len(gen_by_cert),
        "fil_count": len(fil_map),
        "matched": sum(1 for d in details if d["status"] == "一致"),
        "diff": sum(1 for d in details if d["status"] not in ("一致", "仅生成", "仅个税端")),
        "gen_only": sum(1 for d in details if d["status"] == "仅生成"),
        "fil_only": sum(1 for d in details if d["status"] == "仅个税端"),
        "dup_certs": sum(1 for rows in gen_by_cert.values() if len(rows) > 1),
        "same_name_groups": len(same_name),
        "same_name_rows": sum(1 for d in details if d.get("same_name_flag")),
    }
    return {"summary": summary, "details": details, "same_name": same_name}


def export_comparison(month, gen_item_type, filing_item_type, output_dir):
    """导出比对结果 Excel，返回文件名。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    result = compare_generated_vs_filing(month, gen_item_type, filing_item_type)
    s = result["summary"]
    wb = Workbook()
    ws = wb.active
    ws.title = "汇总"
    summary_rows = [
        ("税款所属期/发放月份", month),
        ("生成文件类型", gen_item_type),
        ("个税端导出类型", filing_item_type),
        ("生成行数", s["gen_rows"]),
        ("生成人数（唯一证件）", s["gen_certs"]),
        ("个税端人数", s["fil_count"]),
        ("一致", s["matched"]),
        ("有差异（收入/三险/单位/姓名/重复）", s["diff"]),
        ("仅生成（个税端无）", s["gen_only"]),
        ("仅个税端（生成侧无）", s["fil_only"]),
        ("生成侧重复证件人数", s["dup_certs"]),
        ("同名不同证/同名多人组数", s["same_name_groups"]),
    ]
    ws.append(["项目", "值"])
    for row in summary_rows:
        ws.append(list(row))
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 48

    headers = ["姓名(生成)", "姓名(个税端)", "证件号码", "生成单位", "个税端单位",
               "生成本期收入", "个税端本期收入", "收入差", "生成三险", "个税端三险", "三险差",
               "个税端个税(应补退)", "状态", "说明"]
    detail = wb.create_sheet("比对明细")
    detail.append(headers)
    for d in result["details"]:
        detail.append([d["gen_name"], d["fil_name"], d["cert_no"], d["gen_unit"], d["fil_unit"],
                       d["gen_income"], d["fil_income"], d["income_diff"],
                       d["gen_insurance"], d["fil_insurance"], d["insurance_diff"],
                       d["fil_tax_due"], d["status"], d["note"]])
    widths = [14, 14, 22, 28, 28, 14, 14, 12, 12, 12, 12, 16, 18, 60]
    for i, w in enumerate(widths, start=1):
        detail.column_dimensions[get_column_letter(i)].width = w

    same = wb.create_sheet("同名不同证")
    same.append(["姓名", "类型", "生成侧证件", "个税端证件", "共同单位",
                 "生成侧明细(证件/单位/收入)", "个税端明细(证件/单位/收入)"])
    for g in result["same_name"]:
        same.append([g["name"], g["type"], g["gen_certs"], g["fil_certs"],
                     g["same_units"], g["gen_detail"], g["fil_detail"]])
    for i, w in enumerate([12, 30, 40, 40, 30, 70, 70], start=1):
        same.column_dimensions[get_column_letter(i)].width = w

    dup = wb.create_sheet("生成侧重复")
    dup.append(["姓名", "证件号码", "行数", "明细(单位/收入/来源文件)"])
    for d in result["details"]:
        if d["status"] == "生成侧重复":
            detail_txt = "；".join(f"{r['unit'] or '无单位'} / {r['income']:.2f} / {r['source_file']}"
                                   for r in d["gen_rows"])
            dup.append([d["gen_name"], d["cert_no"], len(d["gen_rows"]), detail_txt])
    for i, w in enumerate([14, 22, 8, 100], start=1):
        dup.column_dimensions[get_column_letter(i)].width = w

    # 表头样式
    for sheet in (ws, detail, same, dup):
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="DDEBF7")
            cell.alignment = Alignment(horizontal="center", vertical="center")

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    safe_type = gen_item_type.replace("/", "_")
    filename = f"报税后核对_{month}_{safe_type}_{filing_item_type}_{timestamp}.xlsx"
    wb.save(os.path.join(output_dir, filename))
    return filename
