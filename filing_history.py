"""filing_history.py - 历史申报数据库：解析税务局个税端导出的 4 类 .xls 文件并入库

本模块将税务局个税端导出的历史申报文件（税款计算 / 正常工资薪金 / 劳务报酬 /
全年一次性奖金）解析为规范化记录，upsert 到 tax_return.db 的 filing_record 表，
并提供汇总与明细查询函数。

数据源为纯 SQLite + xlrd，不涉及 Oracle 数据库（Oracle 只读，本模块零写入）。
"""
import json
import os
import re
import sqlite3
from datetime import datetime

from tax_return import DB_PATH

# 文件类型标识（按表头内容自动识别，与文件名无关）
TYPE_税款计算 = "税款计算"
TYPE_正常工资薪金 = "正常工资薪金"
TYPE_劳务报酬 = "劳务报酬"
TYPE_全年一次性奖金 = "全年一次性奖金"

# 各类型表头特征列（用于自动识别文件类型）
_DETECTORS = [
    (TYPE_税款计算, "税款所属期起"),
    (TYPE_全年一次性奖金, "全年一次性奖金额"),
    (TYPE_劳务报酬, "允许扣除的税费"),
    (TYPE_正常工资薪金, "累计子女教育"),
]

# 各类型字段映射：目标字段 -> 源表头列名
_FIELD_MAP = {
    TYPE_税款计算: {
        "emp_no": "工号",
        "name": "姓名",
        "id_type": "证件类型",
        "cert_no": "证件号码",
        "month_col": "税款所属期起",
        "income": "本期收入",
        "tax_free": "本期免税收入",
        "pension": "本期基本养老保险费",
        "medical": "本期基本医疗保险费",
        "unemployment": "本期失业保险费",
        "housing": "本期住房公积金",
        "tax_accum": "累计应扣缴税额",
        "tax_paid": "已缴税额",
        "tax_due": "应补(退)税额",
        "remark": "备注",
    },
    TYPE_正常工资薪金: {
        "emp_no": "工号",
        "name": "姓名",
        "id_type": "证件类型",
        "cert_no": "证件号码",
        "month_col": "所得期间起",
        "income": "本期收入",
        "tax_free": "本期免税收入",
        "pension": "基本养老保险费",
        "medical": "基本医疗保险费",
        "unemployment": "失业保险费",
        "housing": "住房公积金",
        "tax_accum": None,  # 无累计应扣缴税额列，置 0
        "tax_paid": "已缴税额",
        "tax_due": None,   # 无应补(退)税额列，置 0
        "remark": "备注",
    },
    TYPE_劳务报酬: {
        "emp_no": "工号",
        "name": "姓名",
        "id_type": "证件类型",
        "cert_no": "证件号码",
        "month_col": "所得期间起",
        "income": "收入",
        "tax_free": "免税收入",
        "pension": None,   # 无三险一金列，置 0
        "medical": None,
        "unemployment": None,
        "housing": None,
        "tax_accum": None,
        "tax_paid": "已缴税额",
        "tax_due": None,
        "remark": "备注",
    },
    TYPE_全年一次性奖金: {
        "emp_no": "工号",
        "name": "姓名",
        "id_type": "证件类型",
        "cert_no": "证件号码",
        "month_col": "所得期间起",
        "income": "全年一次性奖金额",
        "tax_free": "免税收入",
        "pension": None,
        "medical": None,
        "unemployment": None,
        "housing": None,
        "tax_accum": None,
        "tax_paid": "已缴税额",
        "tax_due": None,
        "remark": "备注",
    },
}


def get_db():
    """建立 SQLite 连接（与 tax_return.py 同风格，Row 工厂）。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """创建 filing_record 表（若不存在）。"""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS filing_record (
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
            tax_accum REAL DEFAULT 0,
            tax_paid REAL DEFAULT 0,
            tax_due REAL DEFAULT 0,
            remark TEXT DEFAULT '',
            raw TEXT DEFAULT '{}',
            source_file TEXT DEFAULT '',
            import_time TEXT,
            UNIQUE(cert_no, month, item_type)
        )
    """)
    conn.commit()
    conn.close()


def _as_float(v):
    """安全转 float，失败返回 0.0。"""
    try:
        return float(str(v or 0))
    except (ValueError, TypeError):
        return 0.0


def _as_str(v):
    """安全转字符串，None -> ''。"""
    if v is None:
        return ""
    return str(v)


def _cert_no_str(v):
    """证件号码转字符串，保留前导零、避免科学计数法。"""
    if v is None:
        return ""
    if isinstance(v, float):
        # 数值型证件号：去掉科学计数法，保留整数位
        if v == int(v):
            return str(int(v))
        return str(v)
    return str(v).strip()


def _detect_type(header):
    """根据表头内容识别文件类型，返回类型名或 None。"""
    for ftype, marker in _DETECTORS:
        if marker in header:
            return ftype
    return None


def _parse_month(value):
    """从期间起字符串解析月份（前 6 位数字），失败返回 None。

    兼容 "2025-12-01" / "20251201" / "2025/12/01" 及 xlrd 日期单元格。
    """
    if value is None:
        return None
    if isinstance(value, float):
        # xlrd 日期序列号
        try:
            import xlrd
            dt = xlrd.xldate_as_datetime(value, 0)
            return dt.year * 100 + dt.month
        except Exception:
            return None
    s = str(value).strip()
    m = re.search(r"(\d{4})[-/]?(\d{1,2})", s)
    if m:
        return int(m.group(1)) * 100 + int(m.group(2))
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 6 and digits[:6].isdigit():
        return int(digits[:6])
    return None


def _read_sheet(filepath):
    """读取 .xls 首个工作表，返回 (header_row_index, header_list, rows)。"""
    import xlrd
    wb = xlrd.open_workbook(filepath)
    sh = wb.sheet_by_index(0)
    # 找到包含 证件号码 的表头行（兼容表头上方存在标题行的情况）
    header_row = None
    for r in range(sh.nrows):
        row_vals = [str(sh.cell_value(r, c)).strip() for c in range(sh.ncols)]
        if "证件号码" in row_vals:
            header_row = r
            break
    if header_row is None:
        raise ValueError(f"文件缺少 证件号码 列，无法识别表头: {filepath}")
    header = [str(sh.cell_value(header_row, c)).strip() for c in range(sh.ncols)]
    rows = []
    for r in range(header_row + 1, sh.nrows):
        rows.append([sh.cell_value(r, c) for c in range(sh.ncols)])
    return header, rows


def parse_filing_file(filepath):
    """解析单个税务局导出 .xls 文件，返回规范化记录列表。

    自动按表头内容识别文件类型（与文件名无关）。若 姓名/证件号码 列缺失或
    文件类型无法识别，抛出 ValueError。
    """
    header, rows = _read_sheet(filepath)
    ftype = _detect_type(header)
    if ftype is None:
        raise ValueError(f"无法识别文件类型（表头不匹配已知 4 类）: {filepath}")
    if "姓名" not in header or "证件号码" not in header:
        raise ValueError(f"文件缺少 姓名/证件号码 列: {filepath}")

    field_map = _FIELD_MAP[ftype]
    col_idx = {}
    for target, src in field_map.items():
        if src is None:
            col_idx[target] = None
        elif src in header:
            col_idx[target] = header.index(src)
        else:
            col_idx[target] = None

    records = []
    for row in rows:
        cert_no = _cert_no_str(row[col_idx["cert_no"]] if col_idx["cert_no"] is not None else None)
        if not cert_no:
            continue  # 空证件号码行跳过
        name = _as_str(row[col_idx["name"]] if col_idx["name"] is not None else None).strip()
        month = _parse_month(row[col_idx["month_col"]] if col_idx["month_col"] is not None else None)
        if month is None:
            continue  # 无法推导月份的行跳过

        def _cell(target):
            i = col_idx.get(target)
            if i is None:
                return None
            return row[i]

        income = _as_float(_cell("income"))
        tax_free = _as_float(_cell("tax_free"))
        pension = _as_float(_cell("pension"))
        medical = _as_float(_cell("medical"))
        unemployment = _as_float(_cell("unemployment"))
        housing = _as_float(_cell("housing"))
        insurance = pension + medical + unemployment + housing
        tax_accum = _as_float(_cell("tax_accum"))
        tax_paid = _as_float(_cell("tax_paid"))
        tax_due = _as_float(_cell("tax_due"))
        remark = _as_str(_cell("remark")).strip()

        # raw：完整原始行 {表头: 值}，全字段保留
        raw = {}
        for c, h in enumerate(header):
            raw[h] = row[c] if c < len(row) else None
        raw_json = json.dumps(raw, ensure_ascii=False, default=str)

        records.append({
            "cert_no": cert_no,
            "name": name,
            "emp_no": _as_str(_cell("emp_no")).strip(),
            "id_type": _as_str(_cell("id_type")).strip(),
            "month": month,
            "item_type": ftype,
            "income": income,
            "tax_free": tax_free,
            "pension": pension,
            "medical": medical,
            "unemployment": unemployment,
            "housing": housing,
            "insurance": insurance,
            "tax_accum": tax_accum,
            "tax_paid": tax_paid,
            "tax_due": tax_due,
            "remark": remark,
            "raw": raw_json,
        })
    return records


def import_filing_records(records):
    """upsert 记录到 filing_record 表，返回写入条数。"""
    conn = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    count = 0
    for rec in records:
        conn.execute("""
            INSERT INTO filing_record
            (cert_no, name, emp_no, id_type, month, item_type,
             income, tax_free, pension, medical, unemployment, housing, insurance,
             tax_accum, tax_paid, tax_due, remark, raw, source_file, import_time)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cert_no, month, item_type) DO UPDATE SET
                name=excluded.name,
                emp_no=excluded.emp_no,
                id_type=excluded.id_type,
                income=excluded.income,
                tax_free=excluded.tax_free,
                pension=excluded.pension,
                medical=excluded.medical,
                unemployment=excluded.unemployment,
                housing=excluded.housing,
                insurance=excluded.insurance,
                tax_accum=excluded.tax_accum,
                tax_paid=excluded.tax_paid,
                tax_due=excluded.tax_due,
                remark=excluded.remark,
                raw=excluded.raw,
                source_file=excluded.source_file,
                import_time=excluded.import_time
        """, (
            rec["cert_no"], rec["name"], rec["emp_no"], rec["id_type"],
            rec["month"], rec["item_type"],
            rec["income"], rec["tax_free"], rec["pension"], rec["medical"],
            rec["unemployment"], rec["housing"], rec["insurance"],
            rec["tax_accum"], rec["tax_paid"], rec["tax_due"], rec["remark"],
            rec["raw"], rec.get("source_file", ""), now,
        ))
        count += 1
    conn.commit()
    conn.close()
    return count


def get_filing_summary():
    """按 month,item_type 分组统计记录数，month 降序、item_type 升序。"""
    conn = get_db()
    rows = conn.execute("""
        SELECT month, item_type, COUNT(*) AS count
        FROM filing_record
        GROUP BY month, item_type
        ORDER BY month DESC, item_type
    """).fetchall()
    conn.close()
    return [{"month": r["month"], "item_type": r["item_type"], "count": r["count"]} for r in rows]


def get_filing_records(month=0, item_type="", search="", page=1, page_size=50):
    """分页查询申报记录，返回 {"total": int, "records": [row-dicts]}。

    month>0 精确匹配月份；item_type 非空精确匹配类型；search 模糊匹配
    姓名/证件号码/工号。返回记录不含 raw 字段（保持 payload 精简）。
    """
    conn = get_db()
    where = []
    params = []
    if month and month > 0:
        where.append("month = ?")
        params.append(month)
    if item_type:
        where.append("item_type = ?")
        params.append(item_type)
    if search:
        where.append("(name LIKE ? OR cert_no LIKE ? OR emp_no LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM filing_record{where_sql}", params).fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute(f"""
        SELECT id, cert_no, name, emp_no, id_type, month, item_type,
               income, tax_free, pension, medical, unemployment, housing, insurance,
               tax_accum, tax_paid, tax_due, remark, source_file, import_time
        FROM filing_record{where_sql}
        ORDER BY month DESC, item_type, cert_no
        LIMIT ? OFFSET ?
    """, params + [page_size, offset]).fetchall()
    conn.close()
    return {"total": total, "records": [dict(r) for r in rows]}
