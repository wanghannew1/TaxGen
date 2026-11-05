"""config_db.py - 自建 SQLite 配置数据库

**安全规则**: 禁止在 Oracle (工资业务库) 中创建或修改任何数据/表。
所有应用自身需要持久化的配置数据 (如特殊结算单元排除规则) 一律存储在
本地 SQLite 数据库 (config.db), 与 Oracle 业务数据完全隔离。

Oracle 连接 (db.py) 只做只读查询, 任何写入操作都必须走本模块。
"""
import json
import os
import sqlite3
from typing import List, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.db")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """初始化配置表 (幂等)。

    salary_month_scope: 工资所属月取数范围 (三险一金多月合并规则)。
    'all'(默认) = 取发放月内全部工资所属月并合并 (双月发放自动多月合并);
    'first_1'/'first_2' = 仅取最早 N 个工资所属月 (压月单元如37449, 手工只报最早一月);
    'latest_1'/'latest_2' = 仅取最近 N 个工资所属月 (对称预留)。

    pay_pattern: 发薪模式 (2026-09-10 用户确认)。
    'normal'(默认) = 正常发薪; 'pay_lag' = 压月发 (发放月无当月工资属正常,
    建议按最近一次有三险月的三险报单月); 'bimonthly' = 双月发 (隔月发放、每次发
    两个所属月的工资, 建议多月合并报两个月的三险之和)。
    仅是"告诉系统"该单元发薪规律, 弹窗仍由用户最终判断; 其余不规则单元每次人工判断。

    旧库自动 ALTER TABLE ADD COLUMN 迁移。
    """
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS special_unit_config (
            unit_code INTEGER PRIMARY KEY,
            unit_name TEXT DEFAULT '',
            zero_salary_no_add INTEGER DEFAULT 1,
            exclude_all INTEGER DEFAULT 0,
            salary_month_scope TEXT DEFAULT 'all',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 旧库迁移: 已存在表缺 salary_month_scope 列时补列 (SQLite 单次 ALTER 幂等)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(special_unit_config)").fetchall()}
    if "salary_month_scope" not in cols:
        conn.execute(
            "ALTER TABLE special_unit_config ADD COLUMN salary_month_scope TEXT DEFAULT 'all'")
    if "pay_pattern" not in cols:
        conn.execute(
            "ALTER TABLE special_unit_config ADD COLUMN pay_pattern TEXT DEFAULT 'normal'")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS merge_override (
            cert_no TEXT PRIMARY KEY,
            mode TEXT DEFAULT 'double' CHECK (mode IN ('double', 'single', 'skip')),
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 旧库迁移: 2026-09-10 新增"不报"模式(skip)需扩 CHECK 约束, SQLite 不允许改约束
    # → 重建表并回填旧数据 (幂等: 表已含 skip 则跳过)
    _mro = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='merge_override'").fetchone()
    if _mro and "'skip'" not in (_mro["sql"] or ""):
        conn.execute("ALTER TABLE merge_override RENAME TO merge_override_old")
        conn.execute("""
            CREATE TABLE merge_override (
                cert_no TEXT PRIMARY KEY,
                mode TEXT DEFAULT 'double' CHECK (mode IN ('double', 'single', 'skip')),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            INSERT INTO merge_override (cert_no, mode, updated_at)
            SELECT cert_no, mode, updated_at FROM merge_override_old
        """)
        conn.execute("DROP TABLE merge_override_old")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS zero_override (
            cert_no TEXT PRIMARY KEY,
            mode TEXT DEFAULT 'declare' CHECK (mode IN ('declare', 'skip')),
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS merge_group_tag (
            unit_code INTEGER PRIMARY KEY,
            tags TEXT DEFAULT '[]',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tax_roster (
            cert_no TEXT PRIMARY KEY,
            name TEXT DEFAULT '',
            emp_no TEXT DEFAULT '',
            cert_type TEXT DEFAULT '居民身份证',
            hire_date TEXT DEFAULT '',
            leave_date TEXT DEFAULT '',
            nationality TEXT DEFAULT '',
            source TEXT DEFAULT '境内',
            imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        conn.execute("ALTER TABLE tax_roster ADD COLUMN hire_date TEXT DEFAULT ''")
    except Exception:
        pass
    conn.commit()
    conn.close()


def get_special_units() -> List[dict]:
    """查询特殊结算单元配置列表。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT unit_code, unit_name, zero_salary_no_add, exclude_all, salary_month_scope, pay_pattern "
        "FROM special_unit_config ORDER BY unit_code").fetchall()
    conn.close()
    return [{"code": int(r["unit_code"]), "name": str(r["unit_name"] or ""),
             "zero_salary_no_add": int(r["zero_salary_no_add"] or 0),
             "exclude_all": int(r["exclude_all"] or 0),
             "salary_month_scope": str(r["salary_month_scope"] or "all"),
             "pay_pattern": str(r["pay_pattern"] or "normal")} for r in rows]


def get_scope_map() -> dict:
    """查询结算单元 -> 工资所属月取数范围映射 (仅返回非 'all' 的配置)。

    'all'(默认) = 取发放月内全部工资所属月合并 (双月发放自动多月合并);
    'first_1'/'first_2' = 仅取最早 N 个工资所属月 (压月单元, 手工只报最早一月);
    'latest_1'/'latest_2' = 仅取最近 N 个工资所属月 (对称预留)。
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT unit_code, salary_month_scope FROM special_unit_config "
        "WHERE salary_month_scope IS NOT NULL AND salary_month_scope != 'all'").fetchall()
    conn.close()
    return {int(r["unit_code"]): str(r["salary_month_scope"]) for r in rows}


def add_special_unit(unit_code: int, unit_name: str = "", exclude_all: bool = False,
                     salary_month_scope: str = "all", pay_pattern: str = "normal") -> None:
    """新增特殊结算单元配置。

    exclude_all=True 表示该结算单元完全不增员/不报个税 (不论是否有工资)。
    """
    conn = get_db()
    conn.execute(
        "INSERT INTO special_unit_config (unit_code, unit_name, zero_salary_no_add, exclude_all, salary_month_scope, pay_pattern) "
        "VALUES (?, ?, 1, ?, ?, ?)",
        (unit_code, unit_name, 1 if exclude_all else 0, salary_month_scope, pay_pattern))
    conn.commit()
    conn.close()


def add_special_unit_full(unit_code: int, unit_name: str = "",
                          zero_salary_no_add: int = 1, exclude_all: int = 0,
                          salary_month_scope: str = "all",
                          pay_pattern: str = "normal") -> None:
    """新增特殊结算单元配置 (完整模式参数, 用于导入)。"""
    conn = get_db()
    conn.execute(
        "INSERT INTO special_unit_config (unit_code, unit_name, zero_salary_no_add, exclude_all, salary_month_scope, pay_pattern) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (unit_code, unit_name, zero_salary_no_add, exclude_all, salary_month_scope, pay_pattern))
    conn.commit()
    conn.close()


def upsert_special_unit_full(unit_code: int, unit_name: str = "",
                             zero_salary_no_add: int = 1, exclude_all: int = 0,
                             salary_month_scope: str = "all",
                             pay_pattern: str = "normal") -> None:
    """新增或更新特殊结算单元配置 (合并式导入用)。

    已存在同 code 时仅更新业务字段, 保留 created_at; 不存在则插入。
    """
    conn = get_db()
    conn.execute(
        "INSERT INTO special_unit_config (unit_code, unit_name, zero_salary_no_add, exclude_all, salary_month_scope, pay_pattern) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(unit_code) DO UPDATE SET "
        "unit_name = excluded.unit_name, "
        "zero_salary_no_add = excluded.zero_salary_no_add, "
        "exclude_all = excluded.exclude_all, "
        "salary_month_scope = excluded.salary_month_scope, "
        "pay_pattern = excluded.pay_pattern",
        (unit_code, unit_name, zero_salary_no_add, exclude_all, salary_month_scope, pay_pattern))
    conn.commit()
    conn.close()


def update_special_unit(unit_code: int, exclude_all: Optional[bool] = None,
                        zero_salary_no_add: Optional[bool] = None,
                        salary_month_scope: Optional[str] = None,
                        pay_pattern: Optional[str] = None) -> None:
    """更新特殊结算单元配置的排除模式 (传入的字段才更新)。"""
    sets = []
    binds = [unit_code]
    if exclude_all is not None:
        sets.append("exclude_all = ?")
        binds.insert(len(binds) - 1, 1 if exclude_all else 0)
    if zero_salary_no_add is not None:
        sets.append("zero_salary_no_add = ?")
        binds.insert(len(binds) - 1, 1 if zero_salary_no_add else 0)
    if salary_month_scope is not None:
        sets.append("salary_month_scope = ?")
        binds.insert(len(binds) - 1, salary_month_scope)
    if pay_pattern is not None:
        sets.append("pay_pattern = ?")
        binds.insert(len(binds) - 1, pay_pattern)
    if not sets:
        return
    conn = get_db()
    conn.execute(
        f"UPDATE special_unit_config SET {', '.join(sets)} WHERE unit_code = ?",
        binds)
    conn.commit()
    conn.close()


def delete_special_unit(unit_code: Optional[int]) -> None:
    """删除特殊结算单元配置 (unit_code 为 None 时清空全部)。"""
    conn = get_db()
    if unit_code is None:
        conn.execute("DELETE FROM special_unit_config")
    else:
        conn.execute("DELETE FROM special_unit_config WHERE unit_code = ?", (unit_code,))
    conn.commit()
    conn.close()


def get_zero_salary_unit_codes() -> List[int]:
    """查询工资为0不增员不报税 (zero_salary_no_add=1 且非完全排除) 的结算单元代码。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT unit_code FROM special_unit_config WHERE zero_salary_no_add = 1 AND exclude_all = 0").fetchall()
    conn.close()
    return [int(r["unit_code"]) for r in rows]


def get_excluded_unit_codes() -> List[int]:
    """查询完全排除不增员不报税 (exclude_all=1) 的结算单元代码。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT unit_code FROM special_unit_config WHERE exclude_all = 1").fetchall()
    conn.close()
    return [int(r["unit_code"]) for r in rows]


# ---------------------------------------------------------------------------
# 发薪模式 pay_pattern (2026-09-10 用户确认): 压月发/双月发单元配置
# 让用户告诉系统该单元的发薪规律, 仅影响"建议"; 弹窗仍由用户最终判断。
# 'normal'(默认) = 正常发薪; 'pay_lag' = 压月发 (发放月无当月工资属正常);
# 'bimonthly' = 双月发 (隔月发放, 每次发两个所属月工资, 报两个月三险之和)。
# ---------------------------------------------------------------------------

def get_pay_pattern_map() -> dict:
    """查询结算单元 -> 发薪模式映射 (仅返回非 'normal' 的配置)。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT unit_code, pay_pattern FROM special_unit_config "
        "WHERE pay_pattern IS NOT NULL AND pay_pattern != 'normal'").fetchall()
    conn.close()
    return {int(r["unit_code"]): str(r["pay_pattern"]) for r in rows}


# ---------------------------------------------------------------------------
# merge_override: 三险一金合并规则（多月/单月）用户确认选择持久化
# cert_no 为主键（一人一种处理方式，跨月人员按人记忆，覆盖式更新）
# ---------------------------------------------------------------------------

def get_merge_overrides() -> dict:
    """查询全部持久化的合并规则覆盖，返回 {cert_no: {'mode': ..., 'updated_at': ...}}。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT cert_no, mode, updated_at FROM merge_override").fetchall()
    conn.close()
    return {str(r["cert_no"]): {"mode": str(r["mode"] or "double"),
                                "updated_at": str(r["updated_at"] or "")} for r in rows}


def upsert_merge_overrides(choices: dict) -> None:
    """写入/更新合并规则覆盖（用户"记住本次选择"）。choices: {cert_no: 'double'|'single'|'skip'}。"""
    if not choices:
        return
    conn = get_db()
    for cert_no, mode in choices.items():
        if mode not in ("double", "single", "skip"):
            continue
        conn.execute(
            "INSERT INTO merge_override (cert_no, mode) VALUES (?, ?) "
            "ON CONFLICT(cert_no) DO UPDATE SET mode = excluded.mode, "
            "updated_at = CURRENT_TIMESTAMP",
            (str(cert_no), mode))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# zero_override: 零申报（工资为0人员是否生成零申报）用户确认选择持久化
# cert_no 为主键（一人一种处理方式，覆盖式更新）
# mode: 'declare'=生成零申报 | 'skip'=不生成（跳过该人零申报记录）
# ---------------------------------------------------------------------------

def get_zero_overrides() -> dict:
    """查询全部持久化的零申报选择覆盖，返回 {cert_no: {'mode': ..., 'updated_at': ...}}。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT cert_no, mode, updated_at FROM zero_override").fetchall()
    conn.close()
    return {str(r["cert_no"]): {"mode": str(r["mode"] or "declare"),
                                "updated_at": str(r["updated_at"] or "")} for r in rows}


def upsert_zero_overrides(choices: dict) -> None:
    """写入/更新零申报选择覆盖（用户"记住本次选择"）。choices: {cert_no: 'declare'|'skip'}。"""
    if not choices:
        return
    conn = get_db()
    for cert_no, mode in choices.items():
        if mode not in ("declare", "skip"):
            continue
        conn.execute(
            "INSERT INTO zero_override (cert_no, mode) VALUES (?, ?) "
            "ON CONFLICT(cert_no) DO UPDATE SET mode = excluded.mode, "
            "updated_at = CURRENT_TIMESTAMP",
            (str(cert_no), mode))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# merge_group_tag: 三险一金合并确认界面"组标签" (2026-09-10 用户确认)
# 纯标记功能: 用户在合并确认弹窗给主结算单元组打标签 (如 压月发/隔月发),
# 仅提醒用户该组情况, 与任何后台逻辑/生成逻辑不相关。
# tags 列存 JSON 数组字符串, 如 '["压月发","隔月发"]'。
# ---------------------------------------------------------------------------

def get_merge_group_tags() -> dict:
    """查询全部组标签, 返回 {unit_code: [tag, ...]}。纯标记, 不影响任何逻辑。"""
    conn = get_db()
    rows = conn.execute("SELECT unit_code, tags FROM merge_group_tag").fetchall()
    conn.close()
    result = {}
    for r in rows:
        try:
            tags = json.loads(r["tags"] or "[]")
        except (ValueError, TypeError):
            tags = []
        if isinstance(tags, list):
            result[int(r["unit_code"])] = [str(t) for t in tags if str(t).strip()]
    return result


def set_merge_group_tags(unit_code: int, tags: list) -> None:
    """保存某结算单元的组标签 (整体覆盖)。tags: 字符串列表, 自动去空白去重。"""
    clean = []
    seen = set()
    for t in (tags or []):
        s = str(t).strip()
        if s and s not in seen:
            seen.add(s)
            clean.append(s)
    conn = get_db()
    conn.execute(
        "INSERT INTO merge_group_tag (unit_code, tags, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(unit_code) DO UPDATE SET tags = excluded.tags, "
        "updated_at = CURRENT_TIMESTAMP",
        (unit_code, json.dumps(clean, ensure_ascii=False)))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# tax_roster: 个税端人员信息名单 (2026-09-11 零申报候选扩源)
# 来源: 个税端导出的 境内人员信息列表.xls / 境外人员信息列表.xls。
# 用于零申报确认的"个税端在职无工资"候选 (B1 新签合同未做工资 / B2 在册无痕迹)
# 与增减员比对的在职名单。cert_no (证件号码, 大写) 为主键, 按来源覆盖式导入。
# ---------------------------------------------------------------------------

def upsert_tax_roster(persons: list, source: str = "境内") -> int:
    """整体覆盖式导入某来源的个税端名单。

    先删除该 source 的全部记录再批量插入 (个税端导出是全量快照, 覆盖式更新避免残留)。
    persons: List[dict], 含 cert_no/name/emp_no/cert_type/leave_date/nationality。
    返回写入条数。
    """
    rows = []
    for p in persons or []:
        cert = str(p.get("cert_no") or "").strip().upper()
        if not cert:
            continue
        rows.append((cert, str(p.get("name") or "").strip(),
                     str(p.get("emp_no") or "").strip(),
                     str(p.get("cert_type") or "") or "居民身份证",
                     str(p.get("hire_date") or "").strip(),
                     str(p.get("leave_date") or "").strip(),
                     str(p.get("nationality") or "").strip(),
                     source))
    if not rows:
        return 0
    conn = get_db()
    conn.execute("DELETE FROM tax_roster WHERE source = ?", (source,))
    conn.executemany(
        "INSERT INTO tax_roster (cert_no, name, emp_no, cert_type, hire_date, leave_date, nationality, source, imported_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
        rows)
    conn.commit()
    conn.close()
    return len(rows)


def get_tax_roster() -> list:
    """查询全部个税端名单记录, 返回 dict 列表 (cert_no 大写, 含来源)。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT cert_no, name, emp_no, cert_type, hire_date, leave_date, nationality, source, imported_at "
        "FROM tax_roster ORDER BY source, cert_no").fetchall()
    conn.close()
    return [{"cert_no": str(r["cert_no"]),
             "name": str(r["name"] or ""),
             "emp_no": str(r["emp_no"] or ""),
             "cert_type": str(r["cert_type"] or ""),
             "hire_date": str(r["hire_date"] or ""),
             "leave_date": str(r["leave_date"] or ""),
             "nationality": str(r["nationality"] or ""),
             "source": str(r["source"] or ""),
             "imported_at": str(r["imported_at"] or "")} for r in rows]


def get_tax_roster_status() -> dict:
    """名单导入状态: 是否已导入、总人数、分来源人数、最近导入时间。"""
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM tax_roster").fetchone()[0]
    by_source = {str(r["source"]): int(r["n"])
                 for r in conn.execute(
                     "SELECT source, COUNT(*) AS n FROM tax_roster GROUP BY source").fetchall()}
    latest = conn.execute(
        "SELECT MAX(imported_at) FROM tax_roster").fetchone()[0]
    conn.close()
    return {"imported": total > 0, "total": int(total), "by_source": by_source,
            "latest_import": str(latest or "")}


# ---------------------------------------------------------------------------
# app_config: 全局应用配置 (KV 表)
# 存放非单元级、非个人级的全局开关/参数, 全部持久化到 SQLite (config.db),
# 与 Oracle 业务数据完全隔离。现有 key:
#   filter_empty_cert: 过滤身份证号(AAC002)为空的数据 (默认开, 2026-09-17 新增)
# ---------------------------------------------------------------------------

def get_app_config(key: str, default: str = "") -> str:
    """读取全局 KV 配置值, 不存在时返回 default。"""
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
    conn.close()
    return str(row["value"]) if row and row["value"] is not None else default


def set_app_config(key: str, value: str) -> None:
    """写入全局 KV 配置值 (upsert)。"""
    conn = get_db()
    conn.execute(
        "INSERT INTO app_config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value))
    conn.commit()
    conn.close()


def get_filter_empty_cert() -> bool:
    """身份证号为空的数据过滤开关 (默认开启)。

    数据背景 (2026-09-17 排查): AC01 存在无证件号 (AAC002 为 NULL) 的
    "幽灵主档" (已注销但 TC93 仍每月刷 0 工资 G=1 记录, 如东北师大后勤
    37725 的 5 人), 程序此前会如实生成无证件零申报行, 税务端无法受理。
    开启后取数 SQL 增加 AND ac01.AAC002 IS NOT NULL, 直接过滤这类记录;
    关闭则保持旧行为 (含无证件记录)。
    """
    return get_app_config("filter_empty_cert", "1") == "1"


def set_filter_empty_cert(on: bool) -> None:
    """设置身份证号为空的数据过滤开关 ('1' 开 / '0' 关)。"""
    set_app_config("filter_empty_cert", "1" if on else "0")

# ---------------------------------------------------------------------------
# 报税截止日 (2026-10-19 新增): 每月默认"截止日(含)前报上月, 次日起报当月"。
# 默认 15 (1-15日 → 上月, 16日及以后 → 当月)。税务局可能临时调整申报期截止
# 日 (如延到 20 日), 故开放后台修改, 持久化到 app_config (key='report_deadline_day')。
# ---------------------------------------------------------------------------

_REPORT_DEADLINE_DEFAULT = 15


def get_report_deadline_day() -> int:
    """读取报税截止日 (1-31), 未配置或非法时返回默认 15。"""
    raw = get_app_config("report_deadline_day", "")
    try:
        day = int(str(raw).strip())
    except (TypeError, ValueError):
        return _REPORT_DEADLINE_DEFAULT
    if 1 <= day <= 31:
        return day
    return _REPORT_DEADLINE_DEFAULT


def set_report_deadline_day(day: int) -> None:
    """设置报税截止日 (1-31), 越界/非法抛 ValueError (调用方转 400)。"""
    try:
        day = int(day)
    except (TypeError, ValueError):
        raise ValueError("报税截止日必须为 1-31 的整数")
    if not 1 <= day <= 31:
        raise ValueError("报税截止日必须为 1-31 的整数")
    set_app_config("report_deadline_day", str(day))


# ---------------------------------------------------------------------------
# social_security_history: 历史社保/个税端税款计算数据 (2026-11 新增)
# 业务人员可将个税端导出的 税款计算_工资薪金所得.xls 全量导入本库，
# 方便在"单月/多月"合并口径判断时核对某人某月实际社保缴费值、本期收入、
# 累计收入、已缴税额等，避免 TC93 表数据与社保端不一致导致重复扣除
# 或漏报。全部存入 SQLite (config.db)，与 Oracle 业务数据完全隔离。
# raw_json 字段保存个税端导出文件全部51列的完整原始数据。
# ---------------------------------------------------------------------------

# 个税端 税款计算_工资薪金所得.xls 的 51 列 (列名与文件完全一致, 原样建列)。
# (列名, SQLite类型, 默认值) —— 证件号码为 NOT NULL 无默认, 特殊处理。
_SS_ETAX_COLS = [
    ("工号", "TEXT", ""),
    ("姓名", "TEXT", ""),
    ("证件类型", "TEXT", ""),
    ("证件号码", "TEXT", None),
    ("税款所属期起", "TEXT", ""),
    ("税款所属期止", "TEXT", ""),
    ("所得项目", "TEXT", ""),
    ("本期收入", "REAL", 0),
    ("本期费用", "REAL", 0),
    ("本期免税收入", "REAL", 0),
    ("本期基本养老保险费", "REAL", 0),
    ("本期基本医疗保险费", "REAL", 0),
    ("本期失业保险费", "REAL", 0),
    ("本期住房公积金", "REAL", 0),
    ("本期企业(职业)年金", "REAL", 0),
    ("本期商业健康保险费", "REAL", 0),
    ("本期税延养老保险费", "REAL", 0),
    ("本期公务交通费用", "REAL", 0),
    ("本期通讯费用", "REAL", 0),
    ("本期律师办案费用", "REAL", 0),
    ("本期住房公积金和年金调整", "REAL", 0),
    ("本期西藏附加减除费用", "REAL", 0),
    ("本期展业成本", "REAL", 0),
    ("本期其他扣除(其他)", "REAL", 0),
    ("累计收入额", "REAL", 0),
    ("累计免税收入", "REAL", 0),
    ("累计减除费用", "REAL", 0),
    ("累计专项扣除", "REAL", 0),
    ("累计子女教育支出扣除", "REAL", 0),
    ("累计继续教育支出扣除", "REAL", 0),
    ("累计住房贷款利息支出扣除", "REAL", 0),
    ("累计住房租金支出扣除", "REAL", 0),
    ("累计赡养老人支出扣除", "REAL", 0),
    ("累计3岁以下婴幼儿照护", "REAL", 0),
    ("累计个人养老金", "REAL", 0),
    ("累计其他扣除", "REAL", 0),
    ("累计准予扣除的捐赠", "REAL", 0),
    ("其他单位累计收入", "REAL", 0),
    ("其他单位累计扣除", "REAL", 0),
    ("其他单位累计减免税额", "REAL", 0),
    ("其他单位累计已缴税额", "REAL", 0),
    ("累计应纳税所得额", "REAL", 0),
    ("税率", "REAL", 0),
    ("速算扣除数", "REAL", 0),
    ("累计应纳税额", "REAL", 0),
    ("累计减免税额", "REAL", 0),
    ("累计协定减免", "REAL", 0),
    ("累计应扣缴税额", "REAL", 0),
    ("已缴税额", "REAL", 0),
    ("应补(退)税额", "REAL", 0),
    ("备注", "TEXT", ""),
]
# 派生/应用列 (非 Excel 列): salary_month 由「税款所属期起」转换供查询/唯一索引
_SS_APP_COLS = [
    ("salary_month", "INTEGER", 0),
    ("unit_code", "INTEGER", 0),
    ("unit_name", "TEXT", ""),
    ("source", "TEXT", "manual"),
    ("imported_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP", None),
    ("raw_json", "TEXT", ""),
]
_SS_ALL_COLS = _SS_ETAX_COLS + _SS_APP_COLS
# 数值型列集合 (导入时做 float 转换)
_SS_REAL_COLS = {name for name, typ, _ in _SS_ETAX_COLS if typ == "REAL"}

def init_social_security_history() -> None:
    """初始化历史社保/个税端税款计算数据表 (幂等)。

    表结构 = 个税端 税款计算_工资薪金所得.xls 的 51 列列名原样 + 派生/应用列。
    - 结构化列名与 Excel 完全一致（含括号列名如 本期企业(职业)年金），
      导入时零映射，杜绝键名不匹配导致的静默丢数。
    - salary_month: 派生列 (202609 整数)，由「税款所属期起」转换，供查询/唯一索引；
      原始日期 '2026-09-01' 同时完整保留在「税款所属期起」列。
    - SQL 标识符统一双引号包裹（中文/括号列名必需）。
    """
    conn = get_db()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(social_security_history)").fetchall()}
    if not cols:
        # 全新建表
        conn.execute("CREATE TABLE social_security_history (\n  " + _ss_table_ddl() + "\n)")
    elif "cert_no" in cols:
        # 旧结构迁移: 中文列归一化旧表 → 个税端原样列名新表
        _migrate_ss_history_old_to_etax(conn, cols)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(social_security_history)").fetchall()}
    elif "id" not in cols:
        # 新列名但缺 id 主键 (中间版本): 重建表补 id, 数据同名拷贝
        conn.execute(
            "CREATE TABLE social_security_history_new (\n  " + _ss_table_ddl() + "\n)")
        names = [name for name, _, _ in _SS_ALL_COLS]
        col_sql = ", ".join(f'"{c}"' for c in names)
        conn.execute(
            f"INSERT INTO social_security_history_new ({col_sql}) "
            f"SELECT {col_sql} FROM social_security_history")
        conn.execute("DROP TABLE social_security_history")
        conn.execute("ALTER TABLE social_security_history_new RENAME TO social_security_history")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(social_security_history)").fetchall()}
    else:
        # 已是新结构: 为旧版本库补加缺失列
        for name, typ, default in _SS_ALL_COLS:
            if name in cols:
                continue
            if default is None:
                conn.execute(f'ALTER TABLE social_security_history ADD COLUMN "{name}" {typ}')
            else:
                lit = f"'{default}'" if isinstance(default, str) else str(default)
                conn.execute(f'ALTER TABLE social_security_history ADD COLUMN "{name}" {typ} DEFAULT {lit}')
    # 唯一索引: 证件号码 + 派生月份 (个税端文件无单位代码, 不含 unit_code)
    old_idx = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_ss_history_unique'").fetchone()
    if old_idx and "cert_no" in (old_idx["sql"] or ""):
        conn.execute("DROP INDEX idx_ss_history_unique")
    conn.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_ss_history_unique '
        'ON social_security_history("证件号码", salary_month)')
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_ss_history_cert_month '
        'ON social_security_history("证件号码", salary_month)')
    conn.commit()
    conn.close()


def _ss_table_ddl() -> str:
    """生成 social_security_history 建表列定义 (含 id 主键, 列名双引号包裹)。"""
    col_defs = ['id INTEGER PRIMARY KEY AUTOINCREMENT']
    for name, typ, default in _SS_ALL_COLS:
        if name == "证件号码":
            col_defs.append(f'"{name}" TEXT NOT NULL')
        elif default is None:
            col_defs.append(f'"{name}" {typ}')
        else:
            lit = f"'{default}'" if isinstance(default, str) else str(default)
            col_defs.append(f'"{name}" {typ} DEFAULT {lit}')
    return ",\n  ".join(col_defs)


def _migrate_ss_history_old_to_etax(conn, old_cols: set) -> None:
    """旧表(归一化中文列/cert_no) → 新表(个税端原样列名) 数据迁移。"""
    # 旧列名 → 新列名 (同名列不列出)
    old_to_new = {
        "cert_no": "证件号码",
        "name": "姓名",
        "cert_type": "证件类型",
        "salary_month_end": "税款所属期止",
        "income_item": "所得项目",
        "本期企业年金": "本期企业(职业)年金",
        "本期其他扣除": "本期其他扣除(其他)",
        "累计子女教育": "累计子女教育支出扣除",
        "累计继续教育": "累计继续教育支出扣除",
        "累计住房贷款利息": "累计住房贷款利息支出扣除",
        "累计住房租金": "累计住房租金支出扣除",
        "累计赡养老人": "累计赡养老人支出扣除",
        "应补退税额": "应补(退)税额",
    }
    new_to_old = {v: k for k, v in old_to_new.items()}
    conn.execute(
        "CREATE TABLE social_security_history_new (\n  " + _ss_table_ddl() + "\n)")
    # 每个新列: 旧表有对应列则拷贝, 否则用默认值
    selects = []
    insert_cols = []
    for name, typ, default in _SS_ALL_COLS:
        insert_cols.append(f'"{name}"')
        old_col = new_to_old.get(name, name)
        if old_col in old_cols:
            selects.append(f'"{old_col}"')
        elif default is None:
            selects.append("''")
        elif isinstance(default, str):
            selects.append(f"'{default}'")
        else:
            selects.append(str(default))
    conn.execute(
        f"INSERT INTO social_security_history_new ({', '.join(insert_cols)}) "
        f"SELECT {', '.join(selects)} FROM social_security_history")
    conn.execute("DROP TABLE social_security_history")
    conn.execute("ALTER TABLE social_security_history_new RENAME TO social_security_history")


def upsert_social_security_history(rows: list, source: str = "excel") -> int:
    """整体覆盖式导入历史社保/个税端税款计算数据 (按 证件号码+salary_month+unit_code 唯一)。

    rows: List[dict]，键名 = 个税端 Excel 列名原样 (证件号码/姓名/本期收入/...)，
          外加 salary_month(派生 202609)、unit_code/unit_name(旧模板)、raw_json。
    先删除所有与即将插入的 (证件号码, salary_month, unit_code) 组合匹配的记录
    (不论 source)，再批量插入 (数据为全量快照)。
    返回写入条数。
    """
    init_social_security_history()
    inserts = []
    dedup_keys = set()
    for r in rows or []:
        cert = str(r.get("证件号码") or r.get("cert_no") or "").strip().upper()
        if not cert:
            continue
        try:
            month = int(r.get("salary_month") or 0)
        except (TypeError, ValueError):
            continue
        if month <= 0:
            continue
        unit_code = int(r.get("unit_code") or 0)
        dedup_key = (cert, month, unit_code)
        if dedup_key in dedup_keys:
            continue
        dedup_keys.add(dedup_key)
        vals = []
        for name, typ, default in _SS_ALL_COLS:
            if name == "imported_at":
                continue  # 由 CURRENT_TIMESTAMP 生成
            val = r.get(name)
            if name in _SS_REAL_COLS:
                try:
                    val = float(val or 0)
                except (TypeError, ValueError):
                    val = 0.0
            elif name == "证件号码":
                val = cert
            elif name == "salary_month":
                val = month
            elif name == "unit_code":
                val = unit_code
            elif name == "source":
                val = source
            else:
                val = str(val if val is not None else "").strip()
            vals.append(val)
        inserts.append(tuple(vals))
    if not inserts:
        return 0
    conn = get_db()
    # 先删除所有与即将插入的 (证件号码, salary_month, unit_code) 匹配的记录（不论 source）
    for cert, month, unit_code in dedup_keys:
        conn.execute(
            'DELETE FROM social_security_history WHERE "证件号码" = ? AND salary_month = ? AND unit_code = ?',
            (cert, month, unit_code))
    insert_cols = [name for name, _, _ in _SS_ALL_COLS if name != "imported_at"]
    col_sql = ", ".join(f'"{c}"' for c in insert_cols)
    placeholders = ",".join("?" for _ in insert_cols)
    # imported_at 不在插入列中, 由表默认值 CURRENT_TIMESTAMP 自动生成
    conn.executemany(
        f"INSERT INTO social_security_history ({col_sql}) VALUES ({placeholders})",
        inserts)
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM social_security_history WHERE source = ?", (source,)).fetchone()[0]
    conn.close()
    return int(count)


def query_social_security_history(cert_no: Optional[str] = None,
                                   salary_month: Optional[int] = None,
                                   unit_code: Optional[int] = None,
                                   limit: int = 200) -> list:
    """查询历史社保/个税端税款计算数据。

    可按证件号、所属月份、结算单元过滤。按 salary_month DESC, 证件号码 排序。
    返回 dict 键名 = 个税端 Excel 列名原样 + id/salary_month/unit_*/source/imported_at/raw_json。
    """
    init_social_security_history()
    conn = get_db()
    select_cols = ["id"] + [f'"{name}"' for name, _, _ in _SS_ALL_COLS]
    sql = f"""
        SELECT {', '.join(select_cols)}
        FROM social_security_history
        WHERE 1=1
    """
    params = []
    if cert_no:
        sql += ' AND UPPER("证件号码") = UPPER(?)'
        params.append(cert_no.strip().upper())
    if salary_month:
        sql += " AND salary_month = ?"
        params.append(int(salary_month))
    if unit_code:
        sql += " AND unit_code = ?"
        params.append(int(unit_code))
    sql += ' ORDER BY salary_month DESC, "证件号码" LIMIT ?'
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    cols = ["id"] + [name for name, _, _ in _SS_ALL_COLS]
    return [dict(zip(cols, r)) for r in rows]


def delete_social_security_history(cert_no: Optional[str] = None,
                                    salary_month: Optional[int] = None,
                                    unit_code: Optional[int] = None,
                                    source: Optional[str] = None,
                                    ids: Optional[list] = None) -> int:
    """删除历史社保数据。可按条件过滤删除；全部为空时清空全表。

    ids: 按主键 ID 列表删除（优先级最高）。
    """
    init_social_security_history()
    conn = get_db()
    if ids:
        placeholders = ",".join("?" for _ in ids)
        cursor = conn.execute(
            f"DELETE FROM social_security_history WHERE id IN ({placeholders})", [int(i) for i in ids])
    else:
        sql = "DELETE FROM social_security_history WHERE 1=1"
        params = []
        if cert_no:
            sql += ' AND UPPER("证件号码") = UPPER(?)'
            params.append(cert_no.strip().upper())
        if salary_month:
            sql += " AND salary_month = ?"
            params.append(int(salary_month))
        if unit_code:
            sql += " AND unit_code = ?"
            params.append(int(unit_code))
        if source:
            sql += " AND source = ?"
            params.append(source)
        cursor = conn.execute(sql, params)
    conn.commit()
    deleted = cursor.rowcount
    conn.close()
    return deleted


def get_social_security_history_status() -> dict:
    """历史社保数据导入状态: 总条数、分来源条数、最近导入时间、涉及月份范围。"""
    init_social_security_history()
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM social_security_history").fetchone()[0]
    by_source = {
        str(r["source"]): int(r["n"])
        for r in conn.execute(
            "SELECT source, COUNT(*) AS n FROM social_security_history GROUP BY source").fetchall()
    }
    latest = conn.execute("SELECT MAX(imported_at) FROM social_security_history").fetchone()[0]
    month_range = conn.execute(
        "SELECT MIN(salary_month) AS min_m, MAX(salary_month) AS max_m FROM social_security_history").fetchone()
    conn.close()
    return {
        "imported": total > 0,
        "total": int(total),
        "by_source": by_source,
        "latest_import": str(latest or ""),
        "month_min": int(month_range[0]) if month_range and month_range[0] else None,
        "month_max": int(month_range[1]) if month_range and month_range[1] else None,
    }
