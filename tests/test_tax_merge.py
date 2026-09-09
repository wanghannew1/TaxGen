"""tax_merge 三险一金合并规则: 单倍人员合并 + 文件驱动判定建议 (纯逻辑, 无需 Oracle)"""
from decimal import Decimal

import pytest

from models import SalaryRecord
from tax_merge import merge_records_by_person
import tax_merge


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    """覆盖 conftest 的 autouse setup_db: 纯逻辑测试不建 Oracle 连接池"""
    yield


def _rec(emp="E1", cert="C1", sm=202605, unit=100, seq="1",
         income=Decimal("1000"), tax=Decimal("0"),
         pension=Decimal("0"), medical=Decimal("0"),
         unemp=Decimal("0"), housing=Decimal("0"),
         tc930=1, be=Decimal("0")):
    r = SalaryRecord()
    r.职工号 = emp
    r.身份证 = cert
    r.姓名 = f"姓{cert}"
    r.工资所属年月 = sm
    r.结算单元 = unit
    r.当月批次 = seq
    r.tc930_id = tc930
    r.工资总额 = income
    r.应发工资 = income
    r.实发工资 = income - tax
    r.个人所得税 = tax
    r.养老个人 = pension
    r.医疗个人 = medical
    r.失业个人 = unemp
    r.公积金个人 = housing
    r.补缴及退款保险金额个人 = be
    return r


class TestMergeSingleCert:
    def test_single_cert_insurance_not_accumulated(self):
        a = _rec(sm=202605, income=Decimal("5000"), tax=Decimal("100"),
                 pension=Decimal("400"), medical=Decimal("100"),
                 unemp=Decimal("15"), housing=Decimal("350"), tc930=1)
        b = _rec(sm=202606, income=Decimal("6000"), tax=Decimal("200"),
                 pension=Decimal("480"), medical=Decimal("120"),
                 unemp=Decimal("18"), housing=Decimal("420"), tc930=2)
        merged = merge_records_by_person([a, b], by_pay_month=True,
                                         single_certs={"C1"})[0]
        assert merged.工资总额 == Decimal("11000")
        assert merged.个人所得税 == Decimal("300")
        assert merged.养老个人 == Decimal("480")   # 只取基准(流水号最大=202606) 不累加
        assert merged.医疗个人 == Decimal("120")
        assert merged.失业个人 == Decimal("18")
        assert merged.公积金个人 == Decimal("420")

    def test_double_cert_insurance_accumulated(self):
        a = _rec(sm=202605, income=Decimal("5000"), tax=Decimal("100"),
                 pension=Decimal("400"), medical=Decimal("100"),
                 unemp=Decimal("15"), housing=Decimal("350"), tc930=1)
        b = _rec(sm=202606, income=Decimal("6000"), tax=Decimal("200"),
                 pension=Decimal("480"), medical=Decimal("120"),
                 unemp=Decimal("18"), housing=Decimal("420"), tc930=2)
        merged = merge_records_by_person([a, b], by_pay_month=True,
                                         single_certs=set())[0]
        assert merged.工资总额 == Decimal("11000")
        assert merged.个人所得税 == Decimal("300")
        assert merged.养老个人 == Decimal("880")   # 未进 single_certs → 照常累加
        assert merged.医疗个人 == Decimal("220")
        assert merged.失业个人 == Decimal("33")
        assert merged.公积金个人 == Decimal("770")

    def test_single_does_not_affect_other_accumulate_fields(self):
        a = _rec(sm=202605, tc930=1, be=Decimal("10"),
                 income=Decimal("1000"))
        b = _rec(sm=202606, tc930=2, be=Decimal("20"),
                 income=Decimal("2000"))
        merged = merge_records_by_person([a, b], by_pay_month=True,
                                         single_certs={"C1"})[0]
        assert merged.补缴及退款保险金额个人 == Decimal("30")  # BE 照常累加

    def test_by_pay_month_false_ignores_single_certs(self):
        # 现状: 按人+所属月分组时 single_certs 不生效 (保持旧行为)
        a = _rec(sm=202605, tc930=1, income=Decimal("1000"),
                 pension=Decimal("400"))
        b = _rec(sm=202606, tc930=2, income=Decimal("2000"),
                 pension=Decimal("480"))
        merged = merge_records_by_person([a, b], by_pay_month=False,
                                         single_certs={"C1"})[0]
        assert merged.工资总额 == Decimal("1000")     # 同人不同月 → 两行, 第一行
        assert merged.养老个人 == Decimal("400")

    def test_single_takes_base_of_latest_tc930(self):
        # 基准取流水号最大 (最新经办), 与 by_pay_month=False 的旧语义一致
        old = _rec(sm=202606, tc930=1, income=Decimal("1000"),
                   pension=Decimal("480"))
        new = _rec(sm=202605, tc930=50, income=Decimal("2000"),
                   pension=Decimal("400"))
        merged = merge_records_by_person([old, new], by_pay_month=True,
                                         single_certs={"C1"})[0]
        assert merged.tc930_id == 50            # 流水号最大 = 基准
        assert merged.养老个人 == Decimal("400")  # 基准记录的三险
        assert merged.工资总额 == Decimal("3000")  # 收入照常累加


class TestBuildSuggestions:
    COMBOS = [{"unit": 100, "salary_month": 202605, "seq": "1",
               "person_count": 2, "total_income": 5000},
              {"unit": 100, "salary_month": 202606, "seq": "1",
               "person_count": 2, "total_income": 6000}]

    @pytest.fixture(autouse=True)
    def patch_sources(self, monkeypatch):
        self.records = []
        self.filing = {}

        def fake_salary(conn, sm):
            return [r for r in self.records if r.工资所属年月 == sm]

        def fake_filing_map(month, item_type="税款计算"):
            return {k: v for k, v in self.filing.items()}

        monkeypatch.setattr(tax_merge, "get_salary_records", fake_salary)
        monkeypatch.setattr(tax_merge, "get_filing_map", fake_filing_map)
        monkeypatch.setattr(tax_merge, "get_merge_overrides", lambda: {})

    def test_cross_month_person_detected(self):
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1),
            _rec(cert="C1", sm=202606, tc930=2),
            _rec(cert="C2", sm=202605, tc930=3),   # 单月 → 非候选
        ]
        res = tax_merge.build_merge_suggestions(None, 202606, self.COMBOS)
        assert len(res["candidates"]) == 1
        assert res["candidates"][0]["cert_no"] == "C1"

    def test_prev_reported_insurance_leads_single(self):
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1, pension=Decimal("400")),
            _rec(cert="C1", sm=202606, tc930=2),
        ]
        self.filing["C1"] = {"income": 5000, "pension": 400.0,
                             "medical": 100.0, "unemployment": 15.0,
                             "housing": 350.0}
        res = tax_merge.build_merge_suggestions(None, 202606, self.COMBOS)
        c = res["candidates"][0]
        assert c["prev_status"] == "有报"
        assert c["suggested"] == "single"
        assert c["confidence"] == "high"
        assert c["prev_insurance"] == 865.0

    def test_prev_zero_income_leads_double(self):
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1),
            _rec(cert="C1", sm=202606, tc930=2),
        ]
        self.filing["C1"] = {"income": 0, "pension": 0.0, "medical": 0.0,
                             "unemployment": 0.0, "housing": 0.0}
        res = tax_merge.build_merge_suggestions(None, 202606, self.COMBOS)
        c = res["candidates"][0]
        assert c["prev_status"] == "零申报"
        assert c["suggested"] == "double"
        assert c["confidence"] == "medium"

    def test_prev_missing_leads_single(self):
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1),
            _rec(cert="C1", sm=202606, tc930=2),
        ]
        self.filing = {}   # 上月未找到
        res = tax_merge.build_merge_suggestions(None, 202606, self.COMBOS)
        c = res["candidates"][0]
        assert c["prev_status"] == "未找到"
        assert c["suggested"] == "single"
        assert c["confidence"] == "high"

    def test_be_flag_overrides_even_zero_prev(self):
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1, be=Decimal("12.34")),
            _rec(cert="C1", sm=202606, tc930=2),
        ]
        self.filing["C1"] = {"income": 0, "pension": 0.0, "medical": 0.0,
                             "unemployment": 0.0, "housing": 0.0}
        res = tax_merge.build_merge_suggestions(None, 202606, self.COMBOS)
        c = res["candidates"][0]
        assert c["be_flag"] is True
        assert c["suggested"] == "single"
        assert "BE" in c["reason"]

    def test_persisted_override_wins(self, monkeypatch):
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1),
            _rec(cert="C1", sm=202606, tc930=2),
        ]
        self.filing["C1"] = {"income": 0, "pension": 0.0, "medical": 0.0,
                             "unemployment": 0.0, "housing": 0.0}
        monkeypatch.setattr(tax_merge, "get_merge_overrides",
                            lambda: {"C1": {"mode": "single", "updated_at": "2026-09-09"}})
        res = tax_merge.build_merge_suggestions(None, 202606, self.COMBOS)
        c = res["candidates"][0]
        assert c["suggested"] == "double"      # 文件驱动建议不变
        assert c["default_chosen"] == "single"  # 持久化覆盖默认选择
        assert c["persisted"] is True

    def test_prev_income_but_no_insurance_treated_reported(self):
        # 边界: 上月档案收入>0 但三险=0 → 按已报处理 (不虚构三险)
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1),
            _rec(cert="C1", sm=202606, tc930=2),
        ]
        self.filing["C1"] = {"income": 8000, "pension": 0.0, "medical": 0.0,
                             "unemployment": 0.0, "housing": 0.0}
        res = tax_merge.build_merge_suggestions(None, 202606, self.COMBOS)
        assert res["candidates"][0]["prev_status"] == "有报"
        assert res["candidates"][0]["suggested"] == "single"

    def test_empty_combos_returns_empty(self):
        res = tax_merge.build_merge_suggestions(None, 202606, [])
        assert res["candidates"] == []

    def test_salary_months_desc_order(self):
        # 所属月列表升序, 上月档 = 最早所属月
        self.records = [
            _rec(cert="C1", sm=202604, tc930=3),
            _rec(cert="C1", sm=202606, tc930=5),
        ]
        res = tax_merge.build_merge_suggestions(None, 202606, [
            {"unit": 100, "salary_month": 202604, "seq": "1"},
            {"unit": 100, "salary_month": 202606, "seq": "1"},
        ])
        c = res["candidates"][0]
        assert c["salary_months"] == [202604, 202606]
        assert c["prev_month"] == 202604