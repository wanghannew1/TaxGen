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
        self.unpaid_pairs = set()
        self.paid_units = set()

        def fake_salary(conn, combos):
            months = {int(c.get("salary_month", 0) or 0) for c in combos}
            return [r for r in self.records if r.工资所属年月 in months]

        monkeypatch.setattr(tax_zero, "get_salary_records_by_combos", fake_salary)
        monkeypatch.setattr(tax_zero, "get_unpaid_salary_cert_months",
                            lambda conn, months: self.unpaid_pairs)
        monkeypatch.setattr(tax_zero, "get_paid_units_in_month",
                            lambda conn, pay_month: self.paid_units)
        monkeypatch.setattr(tax_zero, "get_zero_salary_unit_codes",
                            lambda: [101])       # 101 为配置"工资为0不申报"单元
        monkeypatch.setattr(tax_zero, "get_excluded_unit_codes",
                            lambda: [102])       # 102 为完全排除单元
        monkeypatch.setattr(tax_zero, "get_zero_overrides", lambda: {})
        # B 类系统判定 (2026-09-11 用户确认): 以 TC90 有无合同为准 (AC01 不判定系统管理)
        # 2026-09-12 合并函数: 在系统判定+合同单元/经办人/工资结束 统一走
        # get_person_tc90_info; 默认无 TC90 记录 (不在系统), 各用例经 self.tc90_info 自备
        self.tc90_info = {}

        def fake_tc90(conn):
            return dict(self.tc90_info)

        monkeypatch.setattr("queries.get_person_tc90_info", fake_tc90)
        # 在册信息 (2026-09-11): 默认无 TC93 记录
        monkeypatch.setattr("queries.get_person_system_info",
                            lambda conn, certs: {})

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
        self.unpaid_pairs = {("C1", 202606)}
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS)
        p = res["units"][0]["persons"][0]
        assert p["category"] == tax_zero.CAT_A2_UNPAID
        assert p["suggested"] == "skip"
        assert p["default_chosen"] == "skip"
        assert "未发放" in p["reason"]

    def test_a2_granular_by_salary_month(self):
        # A2 精确到 (cert, 所属月): 同人 5 月已发(A1) 6 月未发(A2), 未发月单独标记
        self.records = [
            _rec(cert="C1", unit=100, sm=202605, income=Decimal("0")),
            _rec(cert="C1", unit=100, sm=202606, income=Decimal("0")),
        ]
        self.unpaid_pairs = {("C1", 202606)}   # 仅 6 月未发
        combos = [{"unit": 100, "salary_month": 202605, "seq": "1"},
                  {"unit": 100, "salary_month": 202606, "seq": "1"}]
        res = tax_zero.build_zero_salary_suggestions(None, 202606, combos)
        assert res["units"] and res["units"][0]["count"] == 2
        p = next(x for x in res["units"][0]["persons"] if x["cert_no"] == "C1")
        assert p["category"] == tax_zero.CAT_A2_UNPAID
        assert p["salary_months"] == [202605, 202606]

    def test_a2_paid_unit_excluded_from_candidates(self):
        # 发放月已发单元 (2026-09-11 规则): 单元发放月有发放 → 做了没发不进零申报候选
        self.records = [_rec(cert="C1", unit=100, income=Decimal("5000"))]
        self.unpaid_pairs = {("C1", 202606)}
        self.paid_units = {100}     # 单元 100 在发放月有已发记录
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS)
        assert res["units"] == []

    def test_roster_b1_new_hire_declare(self, monkeypatch):
        # B1 名单在册近12个月新入职无工资 → 默认 declare 保留在册
        self.records = []
        roster = [_roster(cert="C1", hire="2026-03-01")]
        self.tc90_info = {"C1": {"unit_code": 100, "unit_name": "单元100",
                                 "contract_handlers": [], "salary_end_ym": 0}}
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
        self.tc90_info = {"C1": {"unit_code": 100, "unit_name": "单元100",
                                 "contract_handlers": [], "salary_end_ym": 0}}
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
        self.tc90_info = {"C1": {"unit_code": 101, "unit_name": "单元101",
                                 "contract_handlers": [], "salary_end_ym": 0}}
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
        self.tc90_info = {"C1": {"unit_code": 102, "unit_name": "单元102",
                                 "contract_handlers": [], "salary_end_ym": 0}}
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        assert res["units"] == []

    def test_roster_outside_system_skip(self, monkeypatch):
        # 名单在册但系统查无此人 (TC90 无合同, 人工管理; AC01 不判定) → 默认不生成,
        # 面板体现 (2026-09-11 用户确认: 以 TC90 有无合同判定在系统, 不在系统默认不生成0申报)
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        # 不在系统: tc90_info 不含 C1 (TC90 查无合同, 孙文强/艾丽场景)
        self.tc90_info = {}
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        p = res["units"][0]["persons"][0]
        assert p["category"] == tax_zero.CAT_B_OUTSIDE
        assert p["suggested"] == "skip"
        assert p["default_chosen"] == "skip"
        assert "人工管理" in p["reason"]

    def test_roster_left_salary_end_before_salary_months_skip(self, monkeypatch):
        # 名单在册但在系统工资结束年月(ATC90AV)早于所属年月 → 判定已离职,
        # 默认不生成 + 建议减员 (2026-09-11 用户确认)
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        self.tc90_info = {"C1": {"unit_code": 100, "unit_name": "单元100",
                                 "contract_handlers": [], "salary_end_ym": 202605}}
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        p = res["units"][0]["persons"][0]
        assert p["category"] == tax_zero.CAT_B_LEFT
        assert p["suggested"] == "skip"
        assert p["default_chosen"] == "skip"
        assert "已离职" in p["reason"] and "减员" in p["reason"]

    def test_roster_in_system_salary_end_gte_salary_months_declare(self, monkeypatch):
        # 名单在册且在系统, 工资结束年月 >= 所属年月 (在职) → 维持 B2 默认生成
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        self.tc90_info = {"C1": {"unit_code": 100, "unit_name": "单元100",
                                 "contract_handlers": [], "salary_end_ym": 202607}}
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        p = res["units"][0]["persons"][0]
        assert p["category"] == tax_zero.CAT_B_ROSTER
        assert p["suggested"] == "declare"
        assert "未减员" in p["reason"]

    def test_roster_sys_info_full_display(self, monkeypatch):
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        self.tc90_info = {"C1": {"unit_code": 100, "unit_name": "单元100",
                                 "contract_handlers": ["合同经办人"],
                                 "salary_end_ym": 202607}}
        monkeypatch.setattr("queries.get_person_system_info",
            lambda conn, certs: {"C1": {"unit_code": 100, "unit_name": "单元100",
                                        "last_pay_ym": 202509, "pay_month": 202509,
                                        "last_batch": "1",
                                        "make_handler": "张朦",
                                        "handler": "白云"}})
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        p = res["units"][0]["persons"][0]
        assert p["sys_info"] == "结算单元:单元100(100)；最后发薪:202509批1；工资结束:202607；经办人:白云"

    def test_roster_sys_info_handler_priority(self, monkeypatch):
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        self.tc90_info = {"C1": {"unit_code": 100, "unit_name": "单元100",
                                 "contract_handlers": ["合同经办人"],
                                 "salary_end_ym": 202607}}
        # 经办人(TC8M.AAE019)为空 → 回落做工资经办人(TC93.AAE019)
        monkeypatch.setattr("queries.get_person_system_info",
            lambda conn, certs: {"C1": {"last_pay_ym": 202509, "pay_month": 202509,
                                        "last_batch": "1", "make_handler": "张朦",
                                        "handler": ""}})
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        assert "经办人:张朦" in res["units"][0]["persons"][0]["sys_info"]
        # 只有合同经办人 → 回落合同经办人
        monkeypatch.setattr("queries.get_person_system_info",
            lambda conn, certs: {"C1": {"last_pay_ym": 202509}})
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        assert "经办人:合同经办人" in res["units"][0]["persons"][0]["sys_info"]

    def test_roster_sys_info_no_records(self, monkeypatch):
        # 以 TC90 判定后: TC90 无合同人员 (如 孙文强/杨冰/艾丽, AC01 有但 TC90 无)
        # → b_outside 不在系统管理, 不再出现"系统内无合同/工资记录"矛盾文案
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        # 不在系统: tc90_info 不含 C1 (TC90 查无合同)
        self.tc90_info = {}
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        p = res["units"][0]["persons"][0]
        assert p["category"] == tax_zero.CAT_B_OUTSIDE
        assert p["reason"] == tax_zero.REASON_B_OUTSIDE
        assert "sys_info" not in p

    def test_roster_sys_info_in_system_no_pay_records(self, monkeypatch):
        # 在系统 (TC90 有合同) 但无 TC93 工资记录: sys_info 显示合同单元与合同经办人
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        # TC90 在册但工资结束 ATC90AV 为空 → salary_end_ym=0, 无"工资结束"片段
        self.tc90_info = {"C1": {"unit_code": 100, "unit_name": "单元100",
                                 "contract_handlers": ["合同经办人"],
                                 "salary_end_ym": 0}}
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        p = res["units"][0]["persons"][0]
        assert p["category"] == tax_zero.CAT_B_ROSTER
        assert p["sys_info"] == "结算单元:单元100(100)；经办人:合同经办人"

    def test_roster_sys_info_pay_month_differs(self, monkeypatch):
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        self.tc90_info = {"C1": {"unit_code": 100, "unit_name": "单元100",
                                 "contract_handlers": [], "salary_end_ym": 202607}}
        monkeypatch.setattr("queries.get_person_system_info",
            lambda conn, certs: {"C1": {"unit_code": 100, "unit_name": "单元100",
                                        "last_pay_ym": 202607, "pay_month": 202608,
                                        "last_batch": "2", "handler": "白云"}})
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        p = res["units"][0]["persons"][0]
        assert p["sys_info"] == "结算单元:单元100(100)；最后发薪:202607批2(202608发)；工资结束:202607；经办人:白云"

    def test_roster_sys_info_pay_month_same_omitted(self, monkeypatch):
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        self.tc90_info = {"C1": {"unit_code": 100, "unit_name": "单元100",
                                 "contract_handlers": [], "salary_end_ym": 202607}}
        monkeypatch.setattr("queries.get_person_system_info",
            lambda conn, certs: {"C1": {"unit_code": 100, "unit_name": "单元100",
                                        "last_pay_ym": 202607, "pay_month": 202607,
                                        "last_batch": "1", "handler": "白云"}})
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        p = res["units"][0]["persons"][0]
        assert "202607发" not in p["sys_info"]
        assert p["sys_info"] == "结算单元:单元100(100)；最后发薪:202607批1；工资结束:202607；经办人:白云"

    def test_handler_filter_keeps_matching_roster(self, monkeypatch):
        # 经办人过滤 (2026-09-11 用户需求): B 类候选经办人链含过滤值 → 保留
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        self.tc90_info = {"C1": {"unit_code": 100, "unit_name": "单元100",
                                 "contract_handlers": [], "salary_end_ym": 202607}}
        monkeypatch.setattr("queries.get_person_system_info",
            lambda conn, certs: {"C1": {"handler": "张朦"}})
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster, handler="张朦")
        assert [p["cert_no"] for p in res["units"][0]["persons"]] == ["C1"]

    def test_handler_filter_drops_nonmatching_roster(self, monkeypatch):
        # B 类候选经办人链不含过滤值 → 排除
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        self.tc90_info = {"C1": {"unit_code": 100, "unit_name": "单元100",
                                 "contract_handlers": [], "salary_end_ym": 202607}}
        monkeypatch.setattr("queries.get_person_system_info",
            lambda conn, certs: {"C1": {"handler": "白云"}})
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster, handler="张朦")
        assert res["units"] == []

    def test_handler_filter_drops_outside_system_roster(self, monkeypatch):
        # b_outside 无经办人关联 → 过滤时一并排除
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        self.tc90_info = {}   # 不在系统 → b_outside
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster, handler="张朦")
        assert res["units"] == []

    def test_handler_filter_empty_keeps_all(self, monkeypatch):
        # handler 为空(未填经办人) → 不过滤
        self.records = []
        roster = [_roster(cert="C1", hire="2020-01-01")]
        self.tc90_info = {"C1": {"unit_code": 100, "unit_name": "单元100",
                                 "contract_handlers": ["合同经办人"],
                                 "salary_end_ym": 202607}}
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster, handler="")
        assert [p["cert_no"] for p in res["units"][0]["persons"]] == ["C1"]

    def test_person_display_order_roster_first_outside_last(self, monkeypatch):
        # 单元内人员排序 (2026-09-11 用户确认): 名单在册(b_roster)最前,
        # 中间为 已离职→收入0→未发放; 不在系统(b_outside) 无 TC90 合同 →
        # 独立归入"未关联结算单元"(unit 0) 组
        self.records = [
            _rec(cert="A1", unit=100, income=Decimal("0")),          # A1 收入0
            _rec(cert="A2", unit=100, income=Decimal("0"), sm=202606),  # 同人 A2 未发放
        ]
        # A2 未发放: 需构造 无 TC8M 发放 的对集合
        self.unpaid_pairs = {("A2", 202606)}
        roster = [
            _roster(cert="B1", hire="2019-01-01"),   # b_roster 名单在册
            _roster(cert="B2", hire="2019-01-01"),   # b_outside 不在系统
            _roster(cert="B3", hire="2019-01-01"),   # b_left 已离职
        ]
        # B1/B3 在系统 (TC90 有合同), B2 不在系统; B3 工资结束 202605 → 已离职
        self.tc90_info = {
            "B1": {"unit_code": 100, "unit_name": "单元100",
                   "contract_handlers": [], "salary_end_ym": 0},
            "B3": {"unit_code": 100, "unit_name": "单元100",
                   "contract_handlers": [], "salary_end_ym": 202605},
        }
        res = tax_zero.build_zero_salary_suggestions(None, 202606, self.COMBOS,
                                                     roster=roster)
        units = {u["unit"]: u for u in res["units"]}
        # 不在系统: 独立 unit 0 组, 不与在册人员同组排序
        outside = [p for p in units[0]["persons"] if p["cert_no"] == "B2"]
        assert outside and outside[0]["category"] == tax_zero.CAT_B_OUTSIDE
        persons = units[100]["persons"]
        order = [(p["cert_no"], p["category"]) for p in persons]
        # 单元 100 排序: b_roster 最前, 中间: 已离职(B3) → 收入0(A1) → 未发放(A2)
        assert order[0][0] == "B1" and order[0][1] == tax_zero.CAT_B_ROSTER
        assert [c[0] for c in order[1:]] == ["B3", "A1", "A2"]


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

    def test_unpaid_pairs_drop_only_unpaid_month(self):
        # A2 精确粒度: 同人 6 月已发(正常申报) 7 月未发(剔除), 互不牵连
        records = [
            _rec(cert="C1", unit=100, sm=202606, income=Decimal("5000")),
            _rec(cert="C1", unit=100, sm=202607, income=Decimal("8000")),
        ]
        kept = tax_zero.filter_zero_records(
            records, {}, set(),
            unpaid_pairs={("C1", 202607)})
        assert [r.工资所属年月 for r in kept] == [202606]

    def test_unpaid_pairs_declare_zeroes_only_unpaid_month(self):
        # declare 的未发人员: 未发月保留全零, 已发月仍按原额正常生成
        records = [
            _rec(cert="C1", unit=100, sm=202606, income=Decimal("5000")),
            _rec(cert="C1", unit=100, sm=202607, income=Decimal("8000")),
        ]
        kept = tax_zero.filter_zero_records(
            records, {"C1": "declare"}, set(),
            unpaid_pairs={("C1", 202607)})
        by_month = {r.工资所属年月: r for r in kept}
        assert by_month[202606].工资总额 == Decimal("5000")
        assert by_month[202607].工资总额 == Decimal("0")

    def test_dict_unpaid_pairs_granular(self):
        records = [
            _dict_rec(cert="C1", unit=100, income="5000"),
            dict(_dict_rec(cert="C1", unit=100, income="8000"), ATC931=202607),
        ]
        kept = tax_zero.filter_zero_dicts(
            records, {}, set(),
            unpaid_pairs={("C1", 202607)})
        assert [r["ATC931"] for r in kept] == [202606]

    def test_paid_unit_unpaid_always_dropped_even_declare(self):
        # 发放月已发单元: 做了没发的记录无论 declare 与否一律剔除 (不零申报不按数额申报)
        records = [
            _rec(cert="C1", unit=100, income=Decimal("8000")),   # 未发
            _rec(cert="C2", unit=200, income=Decimal("8000")),   # 未发, 单元无发放 → declare 保留
        ]
        kept = tax_zero.filter_zero_records(
            records, {"C1": "declare", "C2": "declare"}, set(),
            unpaid_pairs={("C1", 202606), ("C2", 202606)},
            paid_units={100})
        assert [r.身份证 for r in kept] == ["C2"]

    def test_dict_paid_unit_unpaid_dropped(self):
        records = [
            _dict_rec(cert="C1", unit=100, income="8000"),
            _dict_rec(cert="C2", unit=200, income="8000"),
        ]
        kept = tax_zero.filter_zero_dicts(
            records, {"C1": "declare", "C2": "declare"}, set(),
            unpaid_pairs={("C1", 202606), ("C2", 202606)},
            paid_units={100})
        assert [r["身份证"] for r in kept] == ["C2"]


class TestBuildRosterZeroRecords:
    """build_roster_zero_records: B 类注入记录取真实 last_pay_ym/last_batch。

    2026-09-12 用户确认: 不再臆造"组合集合最小所属月" (该月该单元未必做了工资表),
    改取该人最后一次真实发放的所属年月-批次; 从未发过工资 (无 TC93 记录) → 年月=0 批次空。
    """

    @pytest.fixture(autouse=True)
    def patch_sources(self, monkeypatch):
        self.contract_map = {}
        self.sys_info = {}

        def fake_contract(conn, certs):
            return dict(self.contract_map)

        def fake_sys_info(conn, certs):
            return dict(self.sys_info)

        monkeypatch.setattr("queries.get_person_units_contract", fake_contract)
        monkeypatch.setattr("queries.get_person_system_info", fake_sys_info)

    def _call(self, month=202606, choices=None, roster=None, excl=set(), checked=set()):
        roster = roster if roster is not None else [_roster(cert="C1")]
        choices = choices if choices is not None else {"C1": "declare"}
        return tax_zero.build_roster_zero_records(None, month, roster, choices, excl, checked)

    def test_uses_last_pay_ym_batch(self):
        # 有历史发放 → 所属月/批次 取 last_pay_ym/last_batch, 非臆造组合最小月
        self.contract_map = {"C1": {"unit_code": 100, "unit_name": "单元100"}}
        self.sys_info = {"C1": {"last_pay_ym": 202604, "last_batch": "2"}}
        rows = self._call()
        assert len(rows) == 1
        assert rows[0].工资所属年月 == 202604
        assert rows[0].当月批次 == "2"
        assert rows[0].工资总额 == Decimal("0")

    def test_no_history_zero_month(self):
        # 从未发过工资 (无 TC93 记录) → 年月=0 批次空, 验证报告备注"无历史发放记录"
        self.contract_map = {"C1": {"unit_code": 100, "unit_name": "单元100"}}
        self.sys_info = {}
        rows = self._call()
        assert len(rows) == 1
        assert rows[0].工资所属年月 == 0
        assert rows[0].当月批次 == ""

    def test_last_batch_empty_kept_ym(self):
        # 有年月但批次为空 → 年月保留, 批次留空
        self.contract_map = {"C1": {"unit_code": 100, "unit_name": "单元100"}}
        self.sys_info = {"C1": {"last_pay_ym": 202605, "last_batch": ""}}
        rows = self._call()
        assert rows[0].工资所属年月 == 202605
        assert rows[0].当月批次 == ""

    def test_pay_month_differs_attached(self):
        # 发放月≠所属月 (如 9月1日发8月工资) → 挂载 发放月 动态属性, 供验证报告小类标注
        self.contract_map = {"C1": {"unit_code": 100, "unit_name": "单元100"}}
        self.sys_info = {"C1": {"last_pay_ym": 202608, "last_batch": "1", "pay_month": 202609}}
        rows = self._call()
        assert getattr(rows[0], "发放月", 0) == 202609

    def test_pay_month_same_not_attached(self):
        # 发放月==所属月 (当月发放) → 不挂载 (无需标注发放月)
        self.contract_map = {"C1": {"unit_code": 100, "unit_name": "单元100"}}
        self.sys_info = {"C1": {"last_pay_ym": 202605, "last_batch": "1", "pay_month": 202605}}
        rows = self._call()
        assert getattr(rows[0], "发放月", 0) == 0

    def test_no_pay_month_not_attached(self):
        # sys_info 无 pay_month (无 TC8M 批次映射) → 不挂载
        self.contract_map = {"C1": {"unit_code": 100, "unit_name": "单元100"}}
        self.sys_info = {"C1": {"last_pay_ym": 202605, "last_batch": "1"}}
        rows = self._call()
        assert getattr(rows[0], "发放月", 0) == 0

    def test_declare_only_included_others_dropped(self):
        # 仅 declare 的人进入注入; skip 的不注入
        self.contract_map = {"C1": {"unit_code": 100, "unit_name": "单元100"},
                             "C2": {"unit_code": 100, "unit_name": "单元100"}}
        self.sys_info = {"C1": {"last_pay_ym": 202605, "last_batch": "1"},
                         "C2": {"last_pay_ym": 202605, "last_batch": "1"}}
        roster = [_roster(cert="C1"), _roster(cert="C2")]
        rows = self._call(choices={"C1": "declare", "C2": "skip"}, roster=roster)
        assert [r.身份证 for r in rows] == ["C1"]

    def test_checked_certs_excluded(self):
        # checked_certs (已在收入表的人) 不重复注入
        self.contract_map = {"C1": {"unit_code": 100, "unit_name": "单元100"}}
        self.sys_info = {"C1": {"last_pay_ym": 202605, "last_batch": "1"}}
        rows = self._call(checked={"C1"})
        assert rows == []

    def test_excl_codes_dropped(self):
        # 合同单元为完全排除单元 → 剔除
        self.contract_map = {"C1": {"unit_code": 102, "unit_name": "单元102"}}
        self.sys_info = {"C1": {"last_pay_ym": 202605, "last_batch": "1"}}
        rows = self._call(excl={102})
        assert rows == []

    def test_leaved_before_month_end_excluded(self):
        # 名单离职日期早于月末 → 剔除
        self.contract_map = {"C1": {"unit_code": 100, "unit_name": "单元100"}}
        self.sys_info = {"C1": {"last_pay_ym": 202605, "last_batch": "1"}}
        roster = [_roster(cert="C1", leave="2026-05-10")]
        rows = self._call(roster=roster)
        assert rows == []

    def test_no_declare_returns_empty(self):
        rows = self._call(choices={})
        assert rows == []


class TestClassifyRowZeroSubcategories:
    """_classify_row 零申报子类标注: A2/A1/B类/B类-次月发放。

    B类-次月发放 (2026-09-12 用户确认): 发放月≠所属月 (如 9月1日才发8月工资) 时,
    单独小类并标注"所属月-批次（发放月发）", 避免"上次发放:202608"误导。
    """

    def _call(self, rec, income=0, raw_certs=set(), unpaid_certs=set()):
        from templates_gen.normal_salary import _classify_row
        return _classify_row(rec, income, set(), {}, raw_certs, unpaid_certs)

    def test_b_pay_month_differs_new_subcategory(self):
        rec = SalaryRecord(身份证="C1", 结算单元名称="长春市公共关系学校",
                           工资所属年月=202608, 当月批次="1")
        rec.发放月 = 202609
        cat, detail = self._call(rec)
        assert cat == "零申报"
        assert "B类: 名单在册无工资（零申报注入）" in detail
        assert "当期无工资发放，次月发放了当期工资" in detail
        assert "长春市公共关系学校-202608-1（202609发）" in detail

    def test_b_pay_month_same_keeps_previous_note(self):
        rec = SalaryRecord(身份证="C1", 结算单元名称="单元100",
                           工资所属年月=202608, 当月批次="1")
        rec.发放月 = 202608
        cat, detail = self._call(rec)
        assert detail == "B类: 名单在册无工资（零申报注入）；当期无未发工资，上次发放:202608-批次1"

    def test_b_no_pay_month_keeps_previous_note(self):
        rec = SalaryRecord(身份证="C1", 结算单元名称="单元100",
                           工资所属年月=202608, 当月批次="1")
        cat, detail = self._call(rec)
        assert detail == "B类: 名单在册无工资（零申报注入）；当期无未发工资，上次发放:202608-批次1"

    def test_b_no_history_note(self):
        rec = SalaryRecord(身份证="C1", 结算单元名称="单元100",
                           工资所属年月=0, 当月批次="")
        cat, detail = self._call(rec)
        assert detail == "B类: 名单在册无工资（零申报注入）；当期无未发工资，无历史发放记录"

    def test_b_batch_empty_pay_month_differs(self):
        rec = SalaryRecord(身份证="C1", 结算单元名称="单元100",
                           工资所属年月=202605, 当月批次="")
        rec.发放月 = 202606
        cat, detail = self._call(rec)
        assert "单元100-202605（202606发）" in detail

    def test_a1_and_a2_unchanged(self):
        rec = SalaryRecord(身份证="C1")
        cat, detail = self._call(rec, raw_certs={"C1"})
        assert detail == "A1: 工资表收入为0"
        cat, detail = self._call(rec, unpaid_certs={"C1"})
        assert detail == "A2: 做了工资当月未发放"