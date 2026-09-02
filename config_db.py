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
    """初始化配置表 (幂等)。"""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS special_unit_config (
            unit_code INTEGER PRIMARY KEY,
            unit_name TEXT DEFAULT '',
            zero_salary_no_add INTEGER DEFAULT 1,
            exclude_all INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def get_special_units() -> List[dict]:
    """查询特殊结算单元配置列表。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT unit_code, unit_name, zero_salary_no_add, exclude_all "
        "FROM special_unit_config ORDER BY unit_code").fetchall()
    conn.close()
    return [{"code": int(r["unit_code"]), "name": str(r["unit_name"] or ""),
             "zero_salary_no_add": int(r["zero_salary_no_add"] or 0),
             "exclude_all": int(r["exclude_all"] or 0)} for r in rows]


def add_special_unit(unit_code: int, unit_name: str = "", exclude_all: bool = False) -> None:
    """新增特殊结算单元配置。

    exclude_all=True 表示该结算单元完全不增员/不报个税 (不论是否有工资)。
    """
    conn = get_db()
    conn.execute(
        "INSERT INTO special_unit_config (unit_code, unit_name, zero_salary_no_add, exclude_all) "
        "VALUES (?, ?, 1, ?)",
        (unit_code, unit_name, 1 if exclude_all else 0))
    conn.commit()
    conn.close()


def add_special_unit_full(unit_code: int, unit_name: str = "",
                          zero_salary_no_add: int = 1, exclude_all: int = 0) -> None:
    """新增特殊结算单元配置 (完整模式参数, 用于导入)。"""
    conn = get_db()
    conn.execute(
        "INSERT INTO special_unit_config (unit_code, unit_name, zero_salary_no_add, exclude_all) "
        "VALUES (?, ?, ?, ?)",
        (unit_code, unit_name, zero_salary_no_add, exclude_all))
    conn.commit()
    conn.close()


def update_special_unit(unit_code: int, exclude_all: Optional[bool] = None,
                        zero_salary_no_add: Optional[bool] = None) -> None:
    """更新特殊结算单元配置的排除模式 (传入的字段才更新)。"""
    sets = []
    binds = [unit_code]
    if exclude_all is not None:
        sets.append("exclude_all = ?")
        binds.insert(len(binds) - 1, 1 if exclude_all else 0)
    if zero_salary_no_add is not None:
        sets.append("zero_salary_no_add = ?")
        binds.insert(len(binds) - 1, 1 if zero_salary_no_add else 0)
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