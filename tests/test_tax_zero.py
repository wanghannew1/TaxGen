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


def _roster(cert="C1", name="名单人", emp_no="R1", hire="2024-01-01", leave="",
            unit=100):
    """构造个税端名单条目 (config_db.tax_roster 行结构)。"""
    return {"cert_no": cert, "name": name, "emp_no": emp_no,
            "hire_date": hire, "leave_date": leave, "unit_code": unit,
            "unit_name": f"单元{unit}"}


class TestBuildSuggestions:
    COMBOS = [{"unit": 100, "salary_month": 202606, "seq": "1"}]

    @pytest.fixture(autouse=True)
    def patch_sources(self, monkeypatch):
        self.records = []
        self.unpaid_certs = set()

        def fake_salary(conn, combos):
            months = {int(c.get("salary_month", 0) or 0) for c in combos}
            return [r for r in self.records if r.工资所属年月 in months]

        monkeypatch.setattr(tax_zero, "get_salary_records_by_combos", fake_salary)
        monkeypatch.setattr(tax_zero, "get_unpaid_salary_persons",
                            lambda conn, months: self.unpaid_certs)
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

    def test_a2_unpaid_always_candidate_suggested_skip(self):
        # A2 做了工资没发: 本期收入>0 也进候选, 默认 skip (无纳税义务)
        self.records = [_rec(cert="C1", unit=100, income=Decimal("5000"))]
        self.unpaid_certs = {"C1"}
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS)
        p = res["units"][0]["persons"][0]
        assert p["category"] == tax_zero.CAT_A2_UNPAID
        assert p["suggested"] == "skip"
        assert p["default_chosen"] == "skip"
        assert "未发放" in p["reason"]

    def test_roster_b1_new_hire_declare(self, monkeypatch):
        # B1 名单在册近12个月新入职无工资 → 默认 declare 保留在册
        self.records = []
        roster = [_roster(cert="C1", hire="2026-03-01")]
        monkeypatch.setattr("queries.get_person_units_contract",
            lambda conn, certs: {"C1": {"unit_code": 100, "unit_name": "单元100"}})
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        u = res["units"][0]
        assert u["unit"] == 100
        p = u["persons"][0]
        assert p["category"] == tax_zero.CAT_B_ROSTER
        assert p["suggested"] == "declare"
        assert "新签合同" in p["reason"]

    def test_roster_b2_existing_declare(self, monkeypatch):
        # B2 名单在册老员工无痕迹 → 默认 declare (未减员保留在册)
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        monkeypatch.setattr("queries.get_person_units_contract",
            lambda conn, certs: {"C1": {"unit_code": 100, "unit_name": "单元100"}})
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        p = res["units"][0]["persons"][0]
        assert p["category"] == tax_zero.CAT_B_ROSTER
        assert p["suggested"] == "declare"
        assert "未减员" in p["reason"]

    def test_roster_leaved_before_pay_month_excluded(self, monkeypatch):
        # 名单离职日期早于发放月月末 → 不作为 B 候选
        self.records = [_rec(cert="C2", unit=100, income=Decimal("5000"))]
        roster = [_roster(cert="C1", hire="2020-01-01", leave="2026-05-10")]
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        assert res["units"] == [u for u in res["units"] if u["unit"] != 100] or \
               all(u["persons"] == {} for u in res["units"])

    def test_roster_cert_already_checked_excluded(self, monkeypatch):
        # TC93 已有记录的在册人员不重复进 B 候选 (checked_certs 剔除)
        self.records = [
            _rec(cert="C1", unit=100, income=Decimal("0")),
            _rec(cert="C1", unit=100, income=Decimal("0")),
        ]
        roster = [_roster(cert="C1", hire="2020-01-01")]
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        p = res["units"][0]["persons"][0]
        assert p["category"] != tax_zero.CAT_B_ROSTER  # 已按 TC93 记录走 A 类

    def test_roster_config_unit_skip(self, monkeypatch):
        # B 候选落在配置单元(101, zero_salary_no_add) → 默认 skip
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        monkeypatch.setattr("queries.get_person_units_contract",
            lambda conn, certs: {"C1": {"unit_code": 101, "unit_name": "单元101"}})
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        p = res["units"][0]["persons"][0]
        assert p["category"] == tax_zero.CAT_B_ROSTER
        assert p["suggested"] == "skip"
        assert "不增员不报税" in p["reason"]

    def test_roster_excluded_unit_dropped(self, monkeypatch):
        # 名单人员合同单元为完全排除单元(102) → 不进候选
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        monkeypatch.setattr("queries.get_person_units_contract",
            lambda conn, certs: {"C1": {"unit_code": 102, "unit_name": "单元102"}})
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        assert res["units"] == []


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