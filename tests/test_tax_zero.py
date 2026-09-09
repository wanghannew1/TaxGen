"""tax_zero 零申报建议与过滤: 本期收入=0 人员生成/不生成零申报 (纯逻辑, 无需 Oracle)"""
from decimal import Decimal

import pytest

from models import SalaryRecord
import tax_zero


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    """覆盖 conftest 的 autouse setup_db: 纯逻辑测试不建 Oracle 连接池"""
    yield


def _rec(emp="E1", cert="C1", sm=202606, unit=100, seq="1",
         income=Decimal("0"), tax_free=Decimal("0"), big_ill=Decimal("0"),
         be=Decimal("0"), cash=Decimal("0"), owe=Decimal("0")):
    r = SalaryRecord()
    r.职工号 = emp
    r.身份证 = cert
    r.姓名 = f"姓{cert}"
    r.工资所属年月 = sm
    r.结算单元 = unit
    r.结算单元名称 = f"单元{unit}"
    r.当月批次 = seq
    r.工资总额 = income
    r.补发3 = tax_free       # ATC936 本次免税
    r.大病险个人 = big_ill    # ATC93BD
    r.补缴及退款保险金额个人 = be   # ATC93BE
    r.个人交纳现金 = cash     # ATC93X3
    r.个人欠款 = owe         # ATC93E
    return r


def _dict_rec(cert="C1", unit=100, income="0", tax_free="0", big_ill="0",
              be="0", cash="0", owe="0"):
    return {"身份证": cert, "AAC001": cert, "ATB930": unit, "ATC931": 202606,
            "ATC937": "1", "ATC93AA": income, "ATC936": tax_free,
            "ATC93BD": big_ill, "ATC93BE": be, "ATC93X3": cash, "ATC93E": owe}


class TestBuildSuggestions:
    COMBOS = [{"unit": 100, "salary_month": 202606, "seq": "1"}]

    @pytest.fixture(autouse=True)
    def patch_sources(self, monkeypatch):
        self.records = []

        def fake_salary(conn, sm):
            return [r for r in self.records if r.工资所属年月 == sm]

        monkeypatch.setattr(tax_zero, "get_salary_records", fake_salary)
        monkeypatch.setattr(tax_zero, "get_zero_salary_unit_codes",
                            lambda: [101])       # 101 为配置"工资为0不申报"单元
        monkeypatch.setattr(tax_zero, "get_excluded_unit_codes",
                            lambda: [102])       # 102 为完全排除单元
        monkeypatch.setattr(tax_zero, "get_zero_overrides", lambda: {})

    def test_zero_income_record_is_candidate(self):
        self.records = [_rec(cert="C1", unit=100, income=Decimal("0"))]
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS)
        assert len(res["units"]) == 1
        unit = res["units"][0]
        assert unit["unit"] == 100
        assert unit["config"] == "normal"
        assert unit["suggested"] == "declare"
        assert unit["count"] == 1
        assert [p["cert_no"] for p in unit["persons"]] == ["C1"]

    def test_nonzero_income_not_candidate(self):
        self.records = [_rec(cert="C1", unit=100, income=Decimal("5000"))]
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS)
        assert res["units"] == []

    def test_salary_positive_but_net_zero_is_candidate(self):
        # 工资总额>0 但欠款冲抵后本期收入=0 (如 5000 工资 - 5000 欠款)
        self.records = [_rec(cert="C1", unit=100, income=Decimal("5000"),
                             owe=Decimal("5000"))]
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS)
        assert len(res["units"]) == 1

    def test_salary_zero_but_cash_positive_not_candidate(self):
        # 应发0 但现金交纳 608.5 → 本期收入 608.5, 不属零申报
        self.records = [_rec(cert="C1", unit=100, income=Decimal("0"),
                             cash=Decimal("608.5"))]
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS)
        assert res["units"] == []

    def test_excluded_unit_never_candidate(self):
        self.records = [_rec(cert="C1", unit=102, income=Decimal("0"))]
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS)
        assert res["units"] == []

    def test_multi_unit_grouping(self):
        self.records = [
            _rec(cert="C1", unit=100, income=Decimal("0")),
            _rec(cert="C2", unit=100, income=Decimal("0")),
            _rec(cert="C3", unit=103, income=Decimal("0")),
        ]
        combos = [{"unit": 100, "salary_month": 202606, "seq": "1"},
                  {"unit": 103, "salary_month": 202606, "seq": "1"}]
        res = tax_zero.build_zero_salary_suggestions(None, 202606, combos)
        units = {u["unit"]: u for u in res["units"]}
        assert set(units) == {100, 103}
        assert len(units[100]["persons"]) == 2
        assert units[103]["count"] == 1

    def test_config_unit_suggested_skip(self):
        # 配置"工资为0不申报"单元: 建议 skip (可按用户选择反转恢复生成)
        self.records = [_rec(cert="C1", unit=101, income=Decimal("0"))]
        res = tax_zero.build_zero_salary_suggestions(
            None, 202606, [{"unit": 101, "salary_month": 202606, "seq": "1"}])
        p = res["units"][0]["persons"][0]
        assert res["units"][0]["suggested"] == "skip"
        assert p["suggested"] == "skip"
        assert p["default_chosen"] == "skip"
        assert p["persisted"] is False
        assert "不增员不报税" in p["reason"]

    def test_persisted_override_reverses_config_unit(self, monkeypatch):
        # 用户持久化 declare → 配置单元人也可恢复生成 (default_chosen 覆盖建议)
        self.records = [_rec(cert="C1", unit=101, income=Decimal("0"))]
        monkeypatch.setattr(tax_zero, "get_zero_overrides",
                            lambda: {"C1": {"mode": "declare", "updated_at": "2026-09-09"}})
        res = tax_zero.build_zero_salary_suggestions(
            None, 202606, [{"unit": 101, "salary_month": 202606, "seq": "1"}])
        p = res["units"][0]["persons"][0]
        assert p["suggested"] == "skip"        # 建议仍按配置
        assert p["default_chosen"] == "declare"  # 默认选择被持久化覆盖
        assert p["persisted"] is True

    def test_empty_combos_returns_empty(self):
        res = tax_zero.build_zero_salary_suggestions(None, 202606, [])
        assert res["units"] == []

    def test_person_aggregates_cross_month(self):
        self.records = [
            _rec(cert="C1", unit=100, sm=202605, income=Decimal("0")),
            _rec(cert="C1", unit=100, sm=202606, income=Decimal("0")),
        ]
        combos = [{"unit": 100, "salary_month": 202605, "seq": "1"},
                  {"unit": 100, "salary_month": 202606, "seq": "1"}]
        res = tax_zero.build_zero_salary_suggestions(None, 202606, combos)
        p = res["units"][0]["persons"][0]
        assert p["salary_months"] == [202605, 202606]
        assert res["units"][0]["count"] == 2


class TestFilterZero:
    def test_skip_drops_zero_income_records_only(self):
        records = [
            _rec(cert="C1", unit=100, income=Decimal("0")),     # 0收入 skip → 剔除
            _rec(cert="C1", unit=100, income=Decimal("5000")),  # 同人非0收入 → 保留
            _rec(cert="C2", unit=100, income=Decimal("0")),     # 未表态 → 保留
        ]
        kept = tax_zero.filter_zero_records(records, {"C1": "skip"}, set())
        assert [r.身份证 for r in kept] == ["C1", "C2"] or [r.身份证 for r in kept] == ["C2", "C1"]
        assert all(r.工资总额 != 0 or r.身份证 == "C2" for r in kept)

    def test_declare_keeps_all(self):
        records = [
            _rec(cert="C1", unit=100, income=Decimal("0")),
            _rec(cert="C2", unit=100, income=Decimal("0")),
        ]
        kept = tax_zero.filter_zero_records(records, {"C1": "declare", "C2": "declare"}, set())
        assert len(kept) == 2

    def test_excluded_unit_always_dropped(self):
        records = [
            _rec(cert="C1", unit=102, income=Decimal("5000")),  # exclude_all 恒剔除
            _rec(cert="C2", unit=100, income=Decimal("5000")),
        ]
        kept = tax_zero.filter_zero_records(records, {}, {102})
        assert len(kept) == 1
        assert kept[0].身份证 == "C2"

    def test_empty_choices_without_excl_keeps_all(self):
        records = [_rec(cert="C1", unit=100, income=Decimal("0"))]
        kept = tax_zero.filter_zero_records(records, {}, set())
        assert len(kept) == 1

    def test_dict_version_same_semantics(self):
        records = [
            _dict_rec(cert="C1", unit=100, income="0"),          # 0收入 skip → 剔除
            _dict_rec(cert="C2", unit=100, income="0"),          # 未表态 → 保留
            _dict_rec(cert="C3", unit=102, income="5000"),       # exclude_all → 剔除
        ]
        kept = tax_zero.filter_zero_dicts(records, {"C1": "skip"}, {102})
        assert [r["身份证"] for r in kept] == ["C2"]

    def test_dict_income_minus_owe(self):
        # dict 版口径: 工资5000 - 欠款5000 = 0 收入, 选中 skip → 剔除
        records = [_dict_rec(cert="C1", unit=100, income="5000", owe="5000")]
        kept = tax_zero.filter_zero_dicts(records, {"C1": "skip"}, set())
        assert kept == []