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


def _rec(emp="E1", cert="C1", sm=202605, unit=100, seq="1", unit_name="",
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
    r.结算单元名称 = unit_name
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
        self.unit_stats = {}   # {unit: {"people": n, "insured": m}} 默认空 → 全部非主单元

        def fake_salary(conn, combos):
            months = {int(c.get("salary_month", 0) or 0) for c in combos}
            return [r for r in self.records if r.工资所属年月 in months]

        def fake_filing_map(month, item_type="税款计算"):
            return {k: v for k, v in self.filing.items()}

        def fake_unit_stats(conn, units, months):
            return self.unit_stats

        monkeypatch.setattr(tax_merge, "get_salary_records_by_combos", fake_salary)
        monkeypatch.setattr(tax_merge, "get_filing_map", fake_filing_map)
        monkeypatch.setattr(tax_merge, "get_unit_insurance_stats", fake_unit_stats)
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

    def test_slips_carry_amount_fields(self):
        # 每条工资单附本期收入与三险一金金额, 基准记录(流水号最大)标记 is_base
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1, unit=100, unit_name="第一医院",
                 income=Decimal("5000"), pension=Decimal("400"), medical=Decimal("100"),
                 unemp=Decimal("15"), housing=Decimal("350")),
            _rec(cert="C1", sm=202606, tc930=2, unit=100, unit_name="第一医院",
                 income=Decimal("6000"), pension=Decimal("480"), medical=Decimal("120"),
                 unemp=Decimal("18"), housing=Decimal("420")),
        ]
        res = tax_merge.build_merge_suggestions(None, 202606, self.COMBOS)
        slips = res["candidates"][0]["slips"]
        # 按所属月升序: 202605 在前
        assert [(s["salary_month"], s["income"]) for s in slips] == [(202605, 5000.0), (202606, 6000.0)]
        assert [s["pension"] for s in slips] == [400.0, 480.0]
        assert [s["insurance"] for s in slips] == [865.0, 1038.0]  # 四险合计
        assert [s["is_base"] for s in slips] == [False, True]      # 流水号最大 = 基准

    def test_candidate_totals_match_merge_semantics(self):
        # 合计口径与 merge_records_by_person 一致: 收入全加;
        # 三险翻倍=各月全加, 单倍=只取基准记录(tc930_id 最大)的三险
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1,
                 income=Decimal("5000"), pension=Decimal("400"), medical=Decimal("100"),
                 unemp=Decimal("15"), housing=Decimal("350")),
            _rec(cert="C1", sm=202606, tc930=2, unit=100, unit_name="第一医院",
                 income=Decimal("6000"), pension=Decimal("480"), medical=Decimal("120"),
                 unemp=Decimal("18"), housing=Decimal("420")),
        ]
        res = tax_merge.build_merge_suggestions(None, 202606, self.COMBOS)
        c = res["candidates"][0]
        assert c["income_total"] == 11000.0
        assert c["insurance_double"] == 1903.0   # 865 + 1038
        assert c["insurance_single"] == 1038.0   # 基准(202606/tc930=2)的三险

    def test_income_total_always_accumulates_even_single(self):
        # 单倍只影响三险一金, 本期收入仍跨月全加 (与 merge_records_by_person 相同)
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1, income=Decimal("1000")),
            _rec(cert="C1", sm=202606, tc930=2, income=Decimal("2000")),
        ]
        self.filing["C1"] = {"income": 5000, "pension": 400.0, "medical": 100.0,
                             "unemployment": 15.0, "housing": 350.0}
        res = tax_merge.build_merge_suggestions(None, 202606, self.COMBOS)
        c = res["candidates"][0]
        assert c["suggested"] == "single"
        assert c["income_total"] == 3000.0
        assert c["insurance_double"] == 0.0
        assert c["insurance_single"] == 0.0

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

    def test_work_sheets_grouped_by_salary_slip(self):
        # 两级分组: 主结算单元大组, 组内每人列全部工资单明细; 单月非候选(如C3)不进组
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1, unit=100, unit_name="第一医院"),
            _rec(cert="C1", sm=202606, tc930=2, unit=100, unit_name="第一医院"),
            _rec(cert="C2", sm=202605, tc930=3, unit=100, unit_name="第一医院"),
            _rec(cert="C2", sm=202606, tc930=4, unit=100, unit_name="第一医院"),
            _rec(cert="C3", sm=202606, tc930=5, unit=100, unit_name="第一医院"),
        ]
        res = tax_merge.build_merge_suggestions(None, 202606, self.COMBOS)
        sheets = res["work_sheets"]
        assert len(sheets) == 1                    # 同一结算单元跨两个月 → 1 个主单元大组
        g = sheets[0]
        assert (g["unit"], g["count"]) == (100, 2)
        assert g["unit_name"] == "第一医院"        # C3 单月非候选 → 不分组
        certs = sorted(p["cert_no"] for p in g["persons"])
        assert certs == ["C1", "C2"]
        p1 = next(p for p in g["persons"] if p["cert_no"] == "C1")
        slips = [(s["unit"], s["salary_month"]) for s in p1["slips"]]
        assert slips == [(100, 202605), (100, 202606)]

    def test_work_sheets_skip_non_combo_records(self):
        # 同一人跨两个结算单元: 只有勾选组合内的记录进入候选;
        # 主单元为单元级判定 (单元内绝大部分人缴五险一金), 与本人当月保险无关
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1, unit=100, unit_name="A单元",
                 pension=Decimal("400"), medical=Decimal("100"),
                 unemp=Decimal("15"), housing=Decimal("350")),
            _rec(cert="C1", sm=202606, tc930=2, unit=200, unit_name="B单元"),
            _rec(cert="C1", sm=3000, tc930=4, unit=300, unit_name="C单元"),
        ]
        # 单元级覆盖度: A单元(100) 5/10 有人缴 → 非主单元; B单元(200) 9/10 → 主单元
        self.unit_stats = {100: {"people": 10, "insured": 5},
                           200: {"people": 10, "insured": 9}}
        res = tax_merge.build_merge_suggestions(None, 202606, [
            {"unit": 100, "salary_month": 202605, "seq": "1"},
            {"unit": 200, "salary_month": 202606, "seq": "1"},
        ])
        c = res["candidates"][0]
        assert c["units"] == [100, 200]           # 3000 非勾选组合 → 不进候选
        assert c["unit_names"] == {"100": "A单元", "200": "B单元"}
        assert c["main_unit"] == 200              # B单元覆盖度高 → 主单元=200
        assert c["main_unit_name"] == "B单元"
        sheets = res["work_sheets"]
        assert len(sheets) == 1                    # 只按主单元 200 分 1 个大组
        g = sheets[0]
        assert (g["unit"], g["count"]) == (200, 1)
        slips = [(s["unit"], s["salary_month"]) for s in g["persons"][0]["slips"]]
        assert slips == [(100, 202605), (200, 202606)]   # 明细含全部勾选工资单

    def test_main_unit_from_unit_coverage_not_personal(self):
        # 郭颖媛场景: 本人所有记录保险=0, 但某发薪单元历史上绝大部分人缴五险一金
        # → 该单元仍是主单元, 人员归属到它
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1, unit=100, unit_name="吉大二院"),
            _rec(cert="C1", sm=202606, tc930=2, unit=200, unit_name="护士节奖金"),
        ]
        self.unit_stats = {100: {"people": 178, "insured": 177},   # 99%
                           200: {"people": 724, "insured": 0}}
        res = tax_merge.build_merge_suggestions(None, 202606, [
            {"unit": 100, "salary_month": 202605, "seq": "1"},
            {"unit": 200, "salary_month": 202606, "seq": "1"},
        ])
        c = res["candidates"][0]
        assert c["main_unit"] == 100              # 单元级覆盖度高, 与本人当月保险无关
        assert c["main_unit_name"] == "吉大二院"

    def test_main_unit_fallback_earliest_payroll_when_none_main(self):
        # 所有发薪单元都非主单元 (如只在奖金/补贴单元发薪) → 回落最早发薪单元
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1, unit=100, unit_name="助培奖金"),
            _rec(cert="C1", sm=202606, tc930=2, unit=200, unit_name="护士节奖金",
                 pension=Decimal("400")),
        ]
        self.unit_stats = {100: {"people": 484, "insured": 0},
                           200: {"people": 724, "insured": 0}}
        res = tax_merge.build_merge_suggestions(None, 202606, [
            {"unit": 100, "salary_month": 202605, "seq": "1"},
            {"unit": 200, "salary_month": 202606, "seq": "1"},
        ])
        c = res["candidates"][0]
        assert c["main_unit"] == 100              # 全部非主单元 → 最早发薪单元
        assert c["main_unit_name"] == "助培奖金"