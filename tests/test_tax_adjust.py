"""compare_tax 纯逻辑单元测试 (无需真实 Oracle/SQLite 数据)"""
import pytest

import tax_adjust


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    """覆盖 conftest 的 autouse setup_db: 纯逻辑测试不建 Oracle 连接池"""
    yield


# --- Mock 数据源 ---
# system: {cert: {name, tax, unit, unit_keys}}   combos: {(unit,sm,seq): {...}}
# returns: {cert: {name, tax_due, remark}}       unit_names: (unit,sm,seq) -> 名称
SYSTEM = {
    "A001": {"name": "张震东", "tax": 549.68, "unit": 100,
             "unit_keys": {(100, 202604, 1)}},
    "B002": {"name": "周奇奇", "tax": 195.15, "unit": 200,
             "unit_keys": {(200, 202604, 1)}},
    "C003": {"name": "王五", "tax": 100.00, "unit": 100,
             "unit_keys": {(100, 202604, 1)}},
    "D004": {"name": "赵六", "tax": 300.00, "unit": 300,
             "unit_keys": {(300, 202604, 1)}},
}
COMBOS = {
    (100, 202604, 1): {"persons": {"A001", "C003"}, "count": 2},
    (200, 202604, 1): {"persons": {"B002"}, "count": 1},
    (300, 202604, 1): {"persons": {"D004"}, "count": 1},
}
RETURNS = {
    "A001": {"name": "张震东", "tax_due": 62.93, "remark": ""},
    "B002": {"name": "周奇奇", "tax_due": 290.98, "remark": ""},
    "C003": {"name": "王五", "tax_due": 100.00, "remark": ""},
    # E005 仅出现在回盘侧 → 系统无记录
    "E005": {"name": "李四", "tax_due": 88.00, "remark": ""},
}
UNIT_NAMES = {(100, 202604, 1): "单位A", (200, 202604, 1): "单位B",
              (300, 202604, 1): "单位C"}


@pytest.fixture
def patched(monkeypatch):
    def fake_agg(conn, month):
        return SYSTEM, COMBOS

    def fake_unit_names(conn, keys):
        return {k: UNIT_NAMES.get(k, str(k[0])) for k in keys}

    def fake_filing_map(month, item_type="税款计算"):
        assert item_type == "税款计算"
        return RETURNS

    monkeypatch.setattr(tax_adjust, "get_system_aggregate", fake_agg)
    monkeypatch.setattr(tax_adjust, "_get_unit_names", fake_unit_names)
    monkeypatch.setattr(tax_adjust, "get_filing_map", fake_filing_map)
    return tax_adjust.compare_tax(None, 202604)


class TestCompareTax:
    def test_need_refund(self, patched):
        """sys_tax > ret_tax → 需退, 建议下月少扣"""
        d = {x["cert_no"]: x for x in patched["details"]}
        row = d["A001"]
        assert row["status"] == "需退"
        assert row["diff"] == 486.75          # 549.68 - 62.93
        assert row["sys_tax"] == 549.68
        assert row["ret_tax"] == 62.93
        assert row["advice"] == "下月少扣486.75元（退）"
        assert row["unit_name"] == "单位A"
        assert row["unit_periods"] == "202604-批1"

    def test_need_collect(self, patched):
        """sys_tax < ret_tax → 需补, 建议下月多扣"""
        d = {x["cert_no"]: x for x in patched["details"]}
        row = d["B002"]
        assert row["status"] == "需补"
        assert row["diff"] == -95.83          # 195.15 - 290.98
        assert row["advice"] == "下月多扣95.83元（补）"

    def test_equal(self, patched):
        """|diff| < 0.01 → 持平"""
        d = {x["cert_no"]: x for x in patched["details"]}
        row = d["C003"]
        assert row["status"] == "持平"
        assert row["diff"] == 0.0
        assert row["advice"] == "无差异"

    def test_return_missing_system_side(self, patched):
        """系统有、回盘无 → 回盘无记录, 不计入退补"""
        d = {x["cert_no"]: x for x in patched["details"]}
        row = d["D004"]
        assert row["status"] == "回盘无记录"
        assert row["ret_tax"] is None
        assert row["diff"] is None
        assert row["advice"] == "回盘无此人员，无法建议"

    def test_return_extra_system_side(self, patched):
        """回盘有、系统无 → 系统无记录, 计入明细但不计入退补合计"""
        d = {x["cert_no"]: x for x in patched["details"]}
        row = d["E005"]
        assert row["status"] == "系统无记录"
        assert row["sys_tax"] is None
        assert row["diff"] is None
        assert row["advice"] == "系统无此人员，无法建议"

    def test_total_aggregation(self, patched):
        """detail_count 含全部 5 人; 退/补仅统计 需退/需补"""
        t = patched["total"]
        assert t["detail_count"] == 5
        assert t["refund_sum"] == 486.75
        assert t["collect_sum"] == 95.83
        assert t["net"] == 390.92

    def test_units_aggregation(self, patched):
        """仅 |diff|>=0.01 人员入单元汇总, 按 |net| 降序"""
        units = patched["units"]
        assert len(units) == 2
        u100 = next(u for u in units if u["unit"] == 100)
        u200 = next(u for u in units if u["unit"] == 200)
        assert u100["unit_name"] == "单位A"
        assert u100["count"] == 1
        assert u100["refund"] == 486.75
        assert u100["collect"] == 0.0
        assert u100["net"] == 486.75
        assert u200["count"] == 1
        assert u200["collect"] == 95.83
        assert u200["net"] == -95.83
        assert abs(units[0]["net"]) >= abs(units[1]["net"])

    def test_units_sum_equals_total(self, patched):
        """单元汇总退/补之和 == 总退/补"""
        t = patched["total"]
        u_refund = round(sum(u["refund"] for u in patched["units"]), 2)
        u_collect = round(sum(u["collect"] for u in patched["units"]), 2)
        assert u_refund == t["refund_sum"]
        assert u_collect == t["collect_sum"]


class TestCompareTaxEdge:
    def _run(self, monkeypatch, system, returns, combos=None):
        def fake_agg(conn, month):
            return system, (combos or {k: {"persons": {k}, "count": 1}
                                       for k in {k for s in system.values()
                                                 for k in s.get("unit_keys", [])}})
        def fake_unit_names(conn, keys):
            return {k: f"单位{k[0]}" for k in keys}
        def fake_filing_map(month, item_type="税款计算"):
            return returns
        monkeypatch.setattr(tax_adjust, "get_system_aggregate", fake_agg)
        monkeypatch.setattr(tax_adjust, "_get_unit_names", fake_unit_names)
        monkeypatch.setattr(tax_adjust, "get_filing_map", fake_filing_map)
        return tax_adjust.compare_tax(None, 202604)

    def test_empty_both_sides(self, monkeypatch):
        r = self._run(monkeypatch, {}, {})
        assert r["total"]["detail_count"] == 0
        assert r["total"]["refund_sum"] == 0.0
        assert r["total"]["collect_sum"] == 0.0
        assert r["total"]["net"] == 0.0
        assert r["units"] == []
        assert r["details"] == []

    def test_threshold_0_01_is_refund(self, monkeypatch):
        """diff=0.01 边界 → 需退 (非持平)"""
        system = {"A": {"name": "甲", "tax": 100.00, "unit": 1,
                        "unit_keys": {(1, 202604, 1)}}}
        returns = {"A": {"name": "甲", "tax_due": 99.99, "remark": ""}}
        r = self._run(monkeypatch, system, returns)
        assert r["details"][0]["status"] == "需退"
        assert r["details"][0]["diff"] == 0.01
        assert r["total"]["refund_sum"] == 0.01

    def test_multi_key_person(self, monkeypatch):
        """多批次人员: unit_name 用、连接, unit_periods 用 | 连接, 主单元=sorted 首个"""
        system = {
            "M": {"name": "多批次", "tax": 500.00, "unit": 1,
                  "unit_keys": {(1, 202603, 2), (2, 202604, 1)}},
        }
        returns = {"M": {"name": "多批次", "tax_due": 300.00, "remark": ""}}
        r = self._run(monkeypatch, system, returns)
        row = r["details"][0]
        assert row["unit_name"] == "单位1、单位2"
        assert row["unit_periods"] == "202603-批2 | 202604-批1"
        # 主单元 = sorted(keys)[0][0] = (1,202603,2)[0] = 1
        assert r["units"][0]["unit"] == 1
        assert r["units"][0]["refund"] == 200.0