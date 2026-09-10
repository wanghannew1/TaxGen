"""config_db.py - 自建 SQLite 配置数据库

**安全规则**: 禁止在 Oracle (工资业务库) 中创建或修改任何数据/表。
所有应用自身需要持久化的配置数据 (如特殊结算单元排除规则) 一律存储在
本地 SQLite 数据库 (config.db), 与 Oracle 业务数据完全隔离。

Oracle 连接 (db.py) 只做只读查询, 任何写入操作都必须走本模块。
"""
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS merge_override (
            cert_no TEXT PRIMARY KEY,
            mode TEXT DEFAULT 'double' CHECK (mode IN ('double', 'single')),
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS zero_override (
            cert_no TEXT PRIMARY KEY,
            mode TEXT DEFAULT 'declare' CHECK (mode IN ('declare', 'skip')),
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def get_special_units() -> List[dict]:
    """查询特殊结算单元配置列表。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT unit_code, unit_name, zero_salary_no_add, exclude_all, salary_month_scope "
        "FROM special_unit_config ORDER BY unit_code").fetchall()
    conn.close()
    return [{"code": int(r["unit_code"]), "name": str(r["unit_name"] or ""),
             "zero_salary_no_add": int(r["zero_salary_no_add"] or 0),
             "exclude_all": int(r["exclude_all"] or 0),
             "salary_month_scope": str(r["salary_month_scope"] or "all")} for r in rows]


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
                     salary_month_scope: str = "all") -> None:
    """新增特殊结算单元配置。

    exclude_all=True 表示该结算单元完全不增员/不报个税 (不论是否有工资)。
    """
    conn = get_db()
    conn.execute(
        "INSERT INTO special_unit_config (unit_code, unit_name, zero_salary_no_add, exclude_all, salary_month_scope) "
        "VALUES (?, ?, 1, ?, ?)",
        (unit_code, unit_name, 1 if exclude_all else 0, salary_month_scope))
    conn.commit()
    conn.close()


def add_special_unit_full(unit_code: int, unit_name: str = "",
                          zero_salary_no_add: int = 1, exclude_all: int = 0,
                          salary_month_scope: str = "all") -> None:
    """新增特殊结算单元配置 (完整模式参数, 用于导入)。"""
    conn = get_db()
    conn.execute(
        "INSERT INTO special_unit_config (unit_code, unit_name, zero_salary_no_add, exclude_all, salary_month_scope) "
        "VALUES (?, ?, ?, ?, ?)",
        (unit_code, unit_name, zero_salary_no_add, exclude_all, salary_month_scope))
    conn.commit()
    conn.close()


def upsert_special_unit_full(unit_code: int, unit_name: str = "",
                             zero_salary_no_add: int = 1, exclude_all: int = 0,
                             salary_month_scope: str = "all") -> None:
    """新增或更新特殊结算单元配置 (合并式导入用)。

    已存在同 code 时仅更新业务字段, 保留 created_at; 不存在则插入。
    """
    conn = get_db()
    conn.execute(
        "INSERT INTO special_unit_config (unit_code, unit_name, zero_salary_no_add, exclude_all, salary_month_scope) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(unit_code) DO UPDATE SET "
        "unit_name = excluded.unit_name, "
        "zero_salary_no_add = excluded.zero_salary_no_add, "
        "exclude_all = excluded.exclude_all, "
        "salary_month_scope = excluded.salary_month_scope",
        (unit_code, unit_name, zero_salary_no_add, exclude_all, salary_month_scope))
    conn.commit()
    conn.close()


def update_special_unit(unit_code: int, exclude_all: Optional[bool] = None,
                        zero_salary_no_add: Optional[bool] = None,
                        salary_month_scope: Optional[str] = None) -> None:
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
# merge_override: 三险一金合并规则（多月/当月）用户确认选择持久化
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
    """写入/更新合并规则覆盖（用户"记住本次选择"）。choices: {cert_no: 'double'|'single'}。"""
    if not choices:
        return
    conn = get_db()
    for cert_no, mode in choices.items():
        if mode not in ("double", "single"):
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