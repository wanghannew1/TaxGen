"""build_remark_text 纯逻辑单元测试 (无需 Oracle 连接)"""
from templates_gen.normal_salary import build_remark_text


def _c(unit_name, month, seq="1"):
    return {"unit_name": unit_name, "salary_month": month, "seq": seq}


class TestBuildRemarkText:
    """测试收入表备注压缩逻辑: 单单元单批年月合并 / 多组合写全 / 50字符回落"""

    def test_single_unit_single_month_single_batch(self):
        assert build_remark_text([_c("吉林省地质调查院", 202608)]) == "吉林省地质调查院202608-1"

    def test_single_unit_multi_month_single_batch_merges_months(self):
        combos = [_c("吉林省地质调查院", 202608), _c("吉林省地质调查院", 202609)]
        assert build_remark_text(combos) == "吉林省地质调查院202608,202609-1"

    def test_single_unit_single_month_multi_batch_lists_all(self):
        combos = [_c("吉林省地质调查院", 202608, "1"), _c("吉林省地质调查院", 202608, "2")]
        assert build_remark_text(combos) == "吉林省地质调查院202608-1,吉林省地质调查院202608-2"

    def test_multi_unit_multi_batch_writes_all(self):
        combos = [_c("吉林省地质调查院", 202608), _c("东北师范大学人事处", 202608)]
        assert build_remark_text(combos) == "东北师范大学人事处202608-1,吉林省地质调查院202608-1"

    def test_over_50_falls_back_to_unit_names(self):
        combos = [_c(f"结算单元{i:02d}名称较长测试", 202608) for i in range(20)]
        text = build_remark_text(combos)
        assert len(text) <= 50
        assert text.endswith("...")

    def test_extreme_long_names_truncated(self):
        combos = [_c(f"极其非常非常长的结算单元名称{i}", 202608) for i in range(30)]
        text = build_remark_text(combos)
        assert len(text) <= 50
        assert text.endswith("...")

    def test_empty_combos_returns_empty(self):
        assert build_remark_text(None) == ""
        assert build_remark_text([]) == ""

    def test_batch_empty_omits_dash(self):
        combos = [{"unit_name": "吉林省地质调查院", "salary_month": 202608, "seq": ""}]
        assert build_remark_text(combos) == "吉林省地质调查院202608"

    def test_always_under_50_for_random_combos(self):
        import random
        units = ["吉林省地质调查院", "东北师范大学人事处", "吉林大学第二医院B", "长春理工大学"]
        random.seed(42)
        for _ in range(200):
            combos = []
            for _ in range(random.randint(1, 15)):
                combos.append(_c(random.choice(units), random.randint(202601, 202612),
                                 seq=str(random.randint(1, 3))))
            assert len(build_remark_text(combos)) <= 50