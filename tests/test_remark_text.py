"""build_remark_text / 主结算单元判定 纯逻辑单元测试 (无需 Oracle 连接)"""
from templates_gen.normal_salary import build_remark_text, _main_unit_for, _first_pay_unit


def _c(unit_name, month, seq="1", unit=0):
    return {"unit_name": unit_name, "salary_month": month, "seq": seq, "unit": unit}


class TestBuildRemarkText:
    """测试收入表备注逻辑: 只写一个结算单元名称（combos[0]=主单元或当月首次发薪单元），≤50字符"""

    def test_single_unit_single_month_single_batch(self):
        assert build_remark_text([_c("吉林省地质调查院", 202608)]) == "吉林省地质调查院"

    def test_first_is_main_unit_when_cross_unit(self):
        # combos[0] 已按主结算单元/首薪单元优先排序, 备注取第一个
        combos = [_c("吉林省地质调查院", 202608), _c("东北师范大学人事处", 202608)]
        assert build_remark_text(combos) == "吉林省地质调查院"

    def test_long_name_truncated_to_50(self):
        long_name = "吉林省地质调查院长春市朝阳区某某研究院测试中心名称" * 3
        assert len(build_remark_text([_c(long_name, 202608)])) == 50

    def test_empty_combos_returns_empty(self):
        assert build_remark_text(None) == ""
        assert build_remark_text([]) == ""

    def test_only_unit_id_fallback(self):
        combo = {"unit": 37182, "salary_month": 202608, "seq": ""}
        assert build_remark_text([combo]) == "37182"


class TestMainUnitOf:
    """主结算单元判定: 本人发薪单元 ∩ 主单元集(最早发薪) / 回落当月首次发薪单元"""

    def test_intersection_picks_earliest_main_unit(self):
        trips = {(37182, 202607, "1"), (37183, 202606, "2"), (37184, 202607, "3")}
        # 主单元 37182/37183, 取最早发薪(202606)的 37183
        assert _main_unit_for(trips, {37182, 37183}) == 37183

    def test_no_main_unit_falls_back_to_first_pay(self):
        trips = {(37182, 202608, "1"), (37183, 202607, "2")}
        # 无主单元 → 最早发薪(202607)的 37183
        assert _main_unit_for(trips, {}) == 37183
        assert _main_unit_for(trips, {37185}) == 37183

    def test_first_pay_respects_month_then_seq_then_unit(self):
        trips = {(37182, 202608, "1"), (37183, 202608, "1"), (37184, 202607, "5")}
        assert _first_pay_unit(trips) == 37184  # 最早所属月
        trips2 = {(37182, 202608, "2"), (37183, 202608, "1")}
        assert _first_pay_unit(trips2) == 37183  # 同月取最小批次
        trips3 = {(37182, 202608, "1"), (37183, 202608, "1")}
        assert _first_pay_unit(trips3) == 37182  # 同月同批取最小单元代码

    def test_main_unit_equals_first_pay_when_no_intersection(self):
        trips = {(37182, 202608, "1"), (37183, 202607, "2")}
        assert _main_unit_for(trips, None) == _first_pay_unit(trips) == 37183
