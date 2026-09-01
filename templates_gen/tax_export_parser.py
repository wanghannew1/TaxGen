"""个税端导出文件解析器 - 解析境内人员信息列表 .xls

个税端导出的境内人员信息列表为 .xls 格式 (xlrd 解析), 首个 Sheet
为人员数据, 第 0 行为表头。本模块提取增减员比对所需的关键字段,
并生成去重大写的证件号码集合。

字段列号 (实测于 境内人员信息列表1111111111.xls):
- Col 0: 工号, Col 1: 姓名, Col 2: 证件类型, Col 3: 证件号码
- Col 4: 性别, Col 5: 出生日期, Col 7: 报送状态
- Col 9: 手机号码, Col 10: 任职受雇从业日期, Col 11: 离职日期
- Col 14: 是否扣除减除费用, Col 35: 任职受雇从业类型, Col 38: 国籍(地区)
"""
from datetime import datetime
from typing import Dict, List, Set

import xlrd

# 列号常量 (0-based, 与实测导出文件一致)
COL_工号 = 0
COL_姓名 = 1
COL_证件类型 = 2
COL_证件号码 = 3
COL_性别 = 4
COL_出生日期 = 5
COL_报送状态 = 7
COL_手机号码 = 9
COL_任职受雇从业日期 = 10
COL_离职日期 = 11
COL_是否扣除减除费用 = 14
COL_任职受雇从业类型 = 35
COL_国籍 = 38


def _normalize_cell(value, wb=None):
    """将 xlrd 单元格值规范化为字符串。

    - 日期型 (xlrd date) 转为 "YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM:SS"
    - 浮点型 (可能为日期序列号) 尝试按日期转换
    - 其余直接 str()
    """
    if value is None:
        return ""
    if isinstance(value, float) and value == int(value):
        # 整数浮点 (如 123.0) 直接转整数字符串, 避免 "123.0"
        value = int(value)
    if wb is not None and isinstance(value, float) and not value.is_integer():
        try:
            dt = xlrd.xldate_as_datetime(value, wb.datemode)
            return _format_datetime(dt)
        except (ValueError, TypeError):
            pass
    if isinstance(value, float):
        return str(int(value)) if value == int(value) else str(value)
    if isinstance(value, datetime):
        return _format_datetime(value)
    return str(value).strip()


def _format_datetime(dt: datetime) -> str:
    """日期时间格式化: 整点日期输出日期, 否则输出完整时间。"""
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_tax_export(filepath: str) -> List[Dict[str, str]]:
    """解析个税端境内人员信息列表 .xls 文件。

    读取首个 Sheet 全部行 (跳过第 0 行表头), 提取增减员比对所需字段。
    姓名与证件号码均为空的行跳过。返回字段字典列表。
    """
    wb = xlrd.open_workbook(filepath)
    sh = wb.sheet_by_index(0)
    persons = []
    for r in range(1, sh.nrows):
        name = _normalize_cell(sh.cell_value(r, COL_姓名), wb)
        cert = _normalize_cell(sh.cell_value(r, COL_证件号码), wb)
        if not name and not cert:
            continue
        persons.append({
            "工号": _normalize_cell(sh.cell_value(r, COL_工号), wb),
            "姓名": name,
            "证件类型": _normalize_cell(sh.cell_value(r, COL_证件类型), wb),
            "证件号码": cert,
            "性别": _normalize_cell(sh.cell_value(r, COL_性别), wb),
            "出生日期": _normalize_cell(sh.cell_value(r, COL_出生日期), wb),
            "报送状态": _normalize_cell(sh.cell_value(r, COL_报送状态), wb),
            "手机号码": _normalize_cell(sh.cell_value(r, COL_手机号码), wb),
            "任职受雇从业日期": _normalize_cell(sh.cell_value(r, COL_任职受雇从业日期), wb),
            "离职日期": _normalize_cell(sh.cell_value(r, COL_离职日期), wb),
            "是否扣除减除费用": _normalize_cell(sh.cell_value(r, COL_是否扣除减除费用), wb),
            "任职受雇从业类型": _normalize_cell(sh.cell_value(r, COL_任职受雇从业类型), wb),
            "国籍": _normalize_cell(sh.cell_value(r, COL_国籍), wb),
        })
    return persons


def extract_cert_numbers(persons: List[Dict[str, str]]) -> Set[str]:
    """从解析结果提取证件号码集合。

    过滤空值, 统一转大写 (处理末位 X)。
    """
    certs = set()
    for p in persons:
        cert = str(p.get("证件号码") or "").strip().upper()
        if cert:
            certs.add(cert)
    return certs