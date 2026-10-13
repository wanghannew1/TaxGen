"""tax_export_parser 单元测试 (无需 Oracle 连接)

覆盖参差行 (ragged rows) 容错:
- xlrd 对行尾被省略空单元格的行, cell_value(r, c) 在 c >= row_len(r) 时抛 IndexError
  (与真实 xlrd 行为一致, 见 xlrd/sheet.py cell_value)。
- 个税端导出 .xls 被 Excel/LibreOffice 重存后常出现参差行, 解析必须容忍并按空串处理,
  否则整份名单导入失败 ("解析个税端导出文件失败: list index out of range")。
"""
import pytest

from templates_gen import tax_export_parser as t


class _FakeSheet:
    """忠实模拟 xlrd Sheet 的参差行行为: cell_value 越界抛 IndexError。"""

    def __init__(self, rows, ncols):
        self._rows = rows
        self.nrows = len(rows)
        self.ncols = ncols

    def row_len(self, r):
        return len(self._rows[r])

    def cell_value(self, r, c):
        return self._rows[r][c]  # IndexError when c >= row_len (xlrd 行为)


class _FakeBook:
    def __init__(self, sheet):
        self._sheet = sheet
        self.datemode = 0

    def sheet_by_index(self, i):
        return self._sheet


def _full_header():
    hdr = [""] * 52
    hdr[t.COL_工号] = "工号"
    hdr[t.COL_姓名] = "姓名"
    hdr[t.COL_证件类型] = "证件类型"
    hdr[t.COL_证件号码] = "证件号码"
    return hdr


def test_parse_tax_export_tolerates_ragged_data_row(monkeypatch):
    """数据行行尾被省略 (仅前 4 列) → 其余字段按空串, 不抛异常。"""
    rows = [_full_header(), ["EMP1", "张三", "居民身份证", "220101199001011234"]]
    fake = _FakeBook(_FakeSheet(rows, ncols=52))
    monkeypatch.setattr(t.xlrd, "open_workbook", lambda fp: fake)

    persons = t.parse_tax_export("ignored.xls")

    assert len(persons) == 1
    p = persons[0]
    assert p["姓名"] == "张三"
    assert p["证件号码"] == "220101199001011234"
    # 行尾被省略的列 → 空串
    assert p["离职日期"] == ""
    assert p["更新时间"] == ""
    assert p["国籍"] == ""


def test_parse_tax_export_tolerates_ragged_header_row(monkeypatch):
    """表头行行尾被省略 (仅 4 列) 时 is_overseas 探测不抛异常。"""
    hdr = ["工号", "姓名", "证件类型", "证件号码"]
    row = [""] * 52
    row[t.COL_工号] = "EMP2"
    row[t.COL_姓名] = "李四"
    row[t.COL_证件类型] = "居民身份证"
    row[t.COL_证件号码] = "220101199002022345"
    row[t.COL_离职日期] = "2026-09-30"
    fake = _FakeBook(_FakeSheet([hdr, row], ncols=52))
    monkeypatch.setattr(t.xlrd, "open_workbook", lambda fp: fake)

    persons = t.parse_tax_export("ignored.xls")

    assert len(persons) == 1
    assert persons[0]["姓名"] == "李四"
    assert persons[0]["离职日期"] == "2026-09-30"


def test_parse_tax_export_full_row_still_parses(monkeypatch):
    """正常满列行行为不变。"""
    row = [""] * 52
    row[t.COL_工号] = "EMP3"
    row[t.COL_姓名] = "王五"
    row[t.COL_证件类型] = "居民身份证"
    row[t.COL_证件号码] = "220101199003033456"
    row[t.COL_离职日期] = "2026-08-31"
    row[t.COL_国籍] = "中国"
    fake = _FakeBook(_FakeSheet([_full_header(), row], ncols=52))
    monkeypatch.setattr(t.xlrd, "open_workbook", lambda fp: fake)

    persons = t.parse_tax_export("ignored.xls")

    assert len(persons) == 1
    p = persons[0]
    assert p["工号"] == "EMP3"
    assert p["离职日期"] == "2026-08-31"
    assert p["国籍"] == "中国"
