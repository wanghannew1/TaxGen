"""tax_merge 三险一金合并规则: 单月人员合并 + 文件驱动判定建议 (纯逻辑, 无需 Oracle)"""
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

    def test_skip_certs_zeroes_insurance_accumulates_income(self):
        # 不报三险(2026-09-10 用户确认): 三险全按 0 上报, 收入/个税照常跨月累加
        a = _rec(sm=202605, income=Decimal("5000"), tax=Decimal("100"),
                 pension=Decimal("400"), medical=Decimal("100"),
                 unemp=Decimal("15"), housing=Decimal("350"), tc930=1)
        b = _rec(sm=202606, income=Decimal("6000"), tax=Decimal("200"),
                 pension=Decimal("480"), medical=Decimal("120"),
                 unemp=Decimal("18"), housing=Decimal("420"), tc930=2)
        merged = merge_records_by_person([a, b], by_pay_month=True,
                                         skip_certs={"C1"})[0]
        assert merged.工资总额 == Decimal("11000")
        assert merged.个人所得税 == Decimal("300")
        assert merged.养老个人 == Decimal("0")
        assert merged.医疗个人 == Decimal("0")
        assert merged.失业个人 == Decimal("0")
        assert merged.公积金个人 == Decimal("0")

    def test_skip_certs_win_over_single(self):
        # 同时出现在 single_certs 与 skip_certs → 不报优先 (显式覆盖)
        a = _rec(sm=202606, tc930=1, income=Decimal("1000"),
                 pension=Decimal("480"))
        b = _rec(sm=202605, tc930=2, income=Decimal("2000"),
                 pension=Decimal("400"))
        merged = merge_records_by_person([a, b], by_pay_month=True,
                                         single_certs={"C1"}, skip_certs={"C1"})[0]
        assert merged.养老个人 == Decimal("0")
        assert merged.工资总额 == Decimal("3000")

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

    def test_single_takes_cur_month_all_slips(self):
        # 单月=发放月(最新所属月)全部记录的三险之和, 不再只取基准(tc930最大)一条
        # (2026-09-10 IKEMCM/#6: 同月多批次时含三险批次可能不是基准记录 → 漏报)
        old = _rec(sm=202606, tc930=1, income=Decimal("1000"),
                   pension=Decimal("480"))
        new = _rec(sm=202605, tc930=50, income=Decimal("2000"),
                   pension=Decimal("400"))
        merged = merge_records_by_person([old, new], by_pay_month=True,
                                         single_certs={"C1"})[0]
        assert merged.tc930_id == 50            # 流水号最大 = 基准
        assert merged.养老个人 == Decimal("480")  # 单月202606全部记录三险, 而非基准400
        assert merged.工资总额 == Decimal("3000")  # 收入照常累加

    def test_single_falls_back_latest_month_when_no_cur_month(self):
        # 李浩案例 (2026-09-10): 压月发工资, 发放月(202608)没有所属月==发放月的记录,
        # 缺省取最新所属月(202607)的三险, 而非归0
        a = _rec(sm=202606, tc930=1, income=Decimal("9172.00"),
                 pension=Decimal("1336.40"), medical=Decimal("334.10"),
                 unemp=Decimal("50.12"), housing=Decimal("1169.00"))
        b = _rec(sm=202607, tc930=2, income=Decimal("9157.00"),
                 pension=Decimal("1336.40"), medical=Decimal("334.10"),
                 unemp=Decimal("50.12"), housing=Decimal("1169.00"))
        merged = merge_records_by_person([a, b], by_pay_month=True,
                                         single_certs={"C1"}, cur_month=202608)[0]
        assert merged.养老个人 == Decimal("1336.40")   # 单月=最新所属月 202607, 而非0
        assert merged.医疗个人 == Decimal("334.10")
        assert merged.失业个人 == Decimal("50.12")
        assert merged.公积金个人 == Decimal("1169.00")
        assert merged.工资总额 == Decimal("18329.00")  # 收入照常跨月全加

    def test_single_same_month_multi_batch_takes_all(self):
        # 陈百灵案例: 单月202608两批次, 批次1含三险(非基准)批次2基准三险0
        # → 单月=发放月全部记录三险=755.50, 而非基准记录的0
        a = _rec(sm=202608, seq="1", tc930=2, income=Decimal("2357.10"),
                 pension=Decimal("351.46"), medical=Decimal("87.86"),
                 unemp=Decimal("13.18"), housing=Decimal("303.00"))
        b = _rec(sm=202608, seq="2", tc930=3, income=Decimal("10121.00"))
        c = _rec(sm=202606, seq="1", tc930=1, income=Decimal("0"))
        merged = merge_records_by_person([c, a, b], by_pay_month=True,
                                         single_certs={"C1"})[0]
        assert merged.养老个人 == Decimal("351.46")
        assert merged.医疗个人 == Decimal("87.86")
        assert merged.失业个人 == Decimal("13.18")
        assert merged.公积金个人 == Decimal("303.00")
        assert merged.工资总额 == Decimal("12478.10")

    def test_double_same_month_multi_batch_accumulates_all(self):
        # 同一组数据不进 single_certs → 多月, 四险跨月全加
        a = _rec(sm=202608, seq="1", tc930=2, income=Decimal("2357.10"),
                 pension=Decimal("351.46"), medical=Decimal("87.86"),
                 unemp=Decimal("13.18"), housing=Decimal("303.00"))
        b = _rec(sm=202608, seq="2", tc930=3, income=Decimal("10121.00"))
        merged = merge_records_by_person([a, b], by_pay_month=True,
                                         single_certs=set())[0]
        assert merged.养老个人 == Decimal("351.46")  # 同月多笔多月=发放月全部(基准0+其他351.46)


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
            _rec(cert="C1", sm=202605, tc930=1, pension=Decimal("400")),
            _rec(cert="C1", sm=202606, tc930=2),
            _rec(cert="C2", sm=202605, tc930=3, pension=Decimal("400")),   # 单月 → 非候选
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
            _rec(cert="C1", sm=202605, tc930=1, pension=Decimal("400")),
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
            _rec(cert="C1", sm=202605, tc930=1, pension=Decimal("400")),
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
            _rec(cert="C1", sm=202605, tc930=1, be=Decimal("12.34"),
                 pension=Decimal("400")),
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
            _rec(cert="C1", sm=202605, tc930=1, pension=Decimal("400")),
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
            _rec(cert="C1", sm=202605, tc930=1, pension=Decimal("400")),
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
        # 排序: 含三险一金的排最前, 其中所属月==发放月(202606)的第一个 → 202606 在前
        assert [(s["salary_month"], s["income"]) for s in slips] == [(202606, 6000.0), (202605, 5000.0)]
        assert [s["pension"] for s in slips] == [480.0, 400.0]
        assert [s["insurance"] for s in slips] == [1038.0, 865.0]  # 四险合计
        assert [s["is_base"] for s in slips] == [True, False]      # 流水号最大 = 基准

    def test_candidate_totals_match_merge_semantics(self):
        # 合计口径与 merge_records_by_person 一致: 收入全加;
        # 三险多月=各月全加, 单月=发放月(所属月==发放月)全部 slips 的三险
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
        assert c["insurance_single"] == 1038.0   # 单月(202606)全部 slips 的三险

    def test_single_same_month_multi_batch_cur_month_sum(self):
        # 陈百灵弹窗端案例 (IKEMCM/#6): 单月202608两批次都在勾选组合内,
        # 批次1含三险755.50(非基准)批次2基准无三险 → 单月=发放月全部=755.50;
        # 202606 也有三险 → 多月=1511.00, 与单月不同 → 进入候选
        self.records = [
            _rec(cert="C1", sm=202606, tc930=1, unit=100, unit_name="吉大二院B",
                 income=Decimal("0"), pension=Decimal("351.46"),
                 medical=Decimal("87.86"), unemp=Decimal("13.18"),
                 housing=Decimal("303.00")),
            _rec(cert="C1", sm=202608, tc930=2, seq="1", unit=100, unit_name="吉大二院B",
                 income=Decimal("2357.10"), pension=Decimal("351.46"),
                 medical=Decimal("87.86"), unemp=Decimal("13.18"),
                 housing=Decimal("303.00")),
            _rec(cert="C1", sm=202608, tc930=3, seq="2", unit=100, unit_name="吉大二院B",
                 income=Decimal("10121.00")),
        ]
        res = tax_merge.build_merge_suggestions(None, 202608, [
            {"unit": 100, "salary_month": 202606, "seq": "1"},
            {"unit": 100, "salary_month": 202608, "seq": "1"},
            {"unit": 100, "salary_month": 202608, "seq": "2"},
        ])
        c = res["candidates"][0]
        assert c["income_total"] == 12478.10
        assert c["insurance_double"] == 1511.00
        assert c["insurance_single"] == 755.50    # 单月(202608)两批次合计, 不再是基准记录(0)
        assert c["insurance_single_detail"] == {"pension": 351.46, "medical": 87.86,
                                                "unemployment": 13.18, "housing": 303.00}
        assert c["insurance_double_detail"] == {"pension": 702.92, "medical": 175.72,
                                                "unemployment": 26.36, "housing": 606.00}
        # 排序: 含三险的批次1(单月)排最前, 202606(含三险)随后, 无三险的批次2最后
        assert [(s["salary_month"], s["seq"]) for s in c["slips"]] == \
            [(202608, "1"), (202606, "1"), (202608, "2")]

    def test_single_equals_double_skipped(self):
        # 刘斌案例 (2026-09-10): 三险只出现在发放月(202608)这一个月份,
        # 单月==多月==608.50 → 无"合并与否"选择 → 不进候选
        self.records = [
            _rec(cert="C1", sm=202607, tc930=1, unit=40802, unit_name="40802",
                 income=Decimal("1140")),
            _rec(cert="C1", sm=202608, tc930=2, seq="1", unit=40659, unit_name="40659",
                 income=Decimal("2230"), pension=Decimal("351.46"),
                 medical=Decimal("87.86"), unemp=Decimal("13.18"),
                 housing=Decimal("156.00")),
            _rec(cert="C1", sm=202608, tc930=3, seq="2", unit=40659, unit_name="40659",
                 income=Decimal("0")),
            _rec(cert="C1", sm=202608, tc930=4, seq="1", unit=40802, unit_name="40802",
                 income=Decimal("1140")),
        ]
        res = tax_merge.build_merge_suggestions(None, 202608, [
            {"unit": 40802, "salary_month": 202607, "seq": "1"},
            {"unit": 40659, "salary_month": 202608, "seq": "1"},
            {"unit": 40659, "salary_month": 202608, "seq": "2"},
            {"unit": 40802, "salary_month": 202608, "seq": "1"},
        ])
        assert res["candidates"] == []
        assert res["work_sheets"] == []

    def test_income_total_always_accumulates_even_single(self):
        # 单月只影响三险一金, 本期收入仍跨月全加 (与 merge_records_by_person 相同)
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1, income=Decimal("1000"),
                 pension=Decimal("400")),
            _rec(cert="C1", sm=202606, tc930=2, income=Decimal("2000")),
        ]
        self.filing["C1"] = {"income": 5000, "pension": 400.0, "medical": 100.0,
                             "unemployment": 15.0, "housing": 350.0}
        res = tax_merge.build_merge_suggestions(None, 202606, self.COMBOS)
        c = res["candidates"][0]
        assert c["suggested"] == "single"
        assert c["income_total"] == 3000.0
        assert c["insurance_double"] == 400.0
        assert c["insurance_single"] == 0.0

    def test_pay_month_no_record_falls_back_latest_month(self):
        # 李浩案例 (2026-09-10): 压月发工资, 发放月 202608 没有所属月==发放月的工资单,
        # 单月缺省取最新所属月(202607)三险=2889.62, 而非 0;
        # 多月=202607+202606 全加=5779.24
        self.records = [
            _rec(cert="C1", sm=202606, tc930=1, unit=39819, unit_name="铁建宽城",
                 income=Decimal("9172.00"), pension=Decimal("1336.40"),
                 medical=Decimal("334.10"), unemp=Decimal("50.12"),
                 housing=Decimal("1169.00")),
            _rec(cert="C1", sm=202607, tc930=2, unit=39819, unit_name="铁建宽城",
                 income=Decimal("9157.00"), pension=Decimal("1336.40"),
                 medical=Decimal("334.10"), unemp=Decimal("50.12"),
                 housing=Decimal("1169.00")),
        ]
        # 两个所属月都含三险且都≠发放月 → 排序按所属月降序: 202607 在前 202606 在后
        res = tax_merge.build_merge_suggestions(None, 202608, [
            {"unit": 39819, "salary_month": 202606, "seq": "1"},
            {"unit": 39819, "salary_month": 202607, "seq": "1"},
        ])
        c = res["candidates"][0]
        assert c["income_total"] == 18329.00
        assert c["insurance_single"] == 2889.62   # 单月=缺省最新所属月 202607
        assert c["insurance_double"] == 5779.24   # 202607 + 202606
        assert c["insurance_single_detail"] == {"pension": 1336.40, "medical": 334.10,
                                                "unemployment": 50.12, "housing": 1169.00}
        assert [(s["salary_month"], s["seq"]) for s in c["slips"]] == \
            [(202607, "1"), (202606, "1")]

    def test_skip_visibility_switch(self):
        # "不报"选项开关 (2026-09-10 用户确认): 默认全部关闭 → can_skip=False;
        # 全局开 → 压月发人员 can_skip=True; 全局关+单元开 → 仅该单元压月发人员可见;
        # 无关单元开开关 → 不可见; 有"所属月==发放月"工资单 → 即使开关开也不可见
        self.records = [
            _rec(cert="C1", sm=202606, tc930=1, unit=39819, unit_name="铁建宽城",
                 income=Decimal("9172.00"), pension=Decimal("1336.40"),
                 medical=Decimal("334.10"), unemp=Decimal("50.12"),
                 housing=Decimal("1169.00")),
            _rec(cert="C1", sm=202607, tc930=2, unit=39819, unit_name="铁建宽城",
                 income=Decimal("9157.00"), pension=Decimal("1336.40"),
                 medical=Decimal("334.10"), unemp=Decimal("50.12"),
                 housing=Decimal("1169.00")),
        ]
        combos = [
            {"unit": 39819, "salary_month": 202606, "seq": "1"},
            {"unit": 39819, "salary_month": 202607, "seq": "1"},
        ]
        # 默认: 全局关 + 未配置单元 → 不报不可见
        assert tax_merge.build_merge_suggestions(
            None, 202608, combos)["candidates"][0]["can_skip"] is False
        # 全局开 → 压月发可见
        assert tax_merge.build_merge_suggestions(
            None, 202608, combos, skip_global_enabled=True)["candidates"][0]["can_skip"] is True
        # 全局关 + 该单元(39819)开关开 → 可见
        assert tax_merge.build_merge_suggestions(
            None, 202608, combos, skip_unit_codes={39819})["candidates"][0]["can_skip"] is True
        # 全局关 + 无关单元(其他 code)开关开 → 不可见
        assert tax_merge.build_merge_suggestions(
            None, 202608, combos, skip_unit_codes={99999})["candidates"][0]["can_skip"] is False

    def test_skip_hidden_when_has_cur_month_record(self):
        # 发放月内有"所属月==发放月"工资单(正常当月发) → 即使开关全开, 不报也不可见
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1, unit=100, unit_name="A单元",
                 pension=Decimal("400")),
            _rec(cert="C1", sm=202606, tc930=2, unit=100, unit_name="A单元",
                 pension=Decimal("200")),
        ]
        res = tax_merge.build_merge_suggestions(
            None, 202606, self.COMBOS, skip_global_enabled=True)
        assert res["candidates"][0]["can_skip"] is False

    def test_salary_months_desc_order(self):
        # 所属月列表升序, 上月档 = 最早所属月
        self.records = [
            _rec(cert="C1", sm=202604, tc930=3, pension=Decimal("400")),
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
            _rec(cert="C1", sm=202605, tc930=1, unit=100, unit_name="第一医院",
                 pension=Decimal("400")),
            _rec(cert="C1", sm=202606, tc930=2, unit=100, unit_name="第一医院"),
            _rec(cert="C2", sm=202605, tc930=3, unit=100, unit_name="第一医院",
                 pension=Decimal("400")),
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
        # 郭颖媛场景: 本人当月保险=0, 但某发薪单元历史上绝大部分人缴五险一金
        # → 该单元仍是主单元, 人员归属到它 (202605 有保险 → double≠single 保留候选)
        self.records = [
            _rec(cert="C1", sm=202605, tc930=1, unit=100, unit_name="吉大二院",
                 pension=Decimal("400")),
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
            _rec(cert="C1", sm=202605, tc930=1, unit=100, unit_name="助培奖金",
                 pension=Decimal("400")),
            _rec(cert="C1", sm=202606, tc930=2, unit=200, unit_name="护士节奖金"),
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