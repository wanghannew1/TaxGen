"""tax_forecast 合并预判规则 (纯逻辑, 无需 Oracle)。"""
import pytest

from tax_forecast import _empirical_merge_rule


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    """覆盖 conftest 的 autouse setup_db: 纯逻辑测试不建 Oracle 连接池"""
    yield


def _cand(months, incomes, prev_status="有报", prev_income=0, can_skip=False):
    return {
        "salary_months": months,
        "prev_status": prev_status,
        "prev_income": prev_income,
        "can_skip": can_skip,
        "slips": [{"salary_month": m, "income": incomes[i]}
                  for i, m in enumerate(months)],
    }


def test_prev_zero_prefers_double():
    c = _cand([202608, 202609], [5000, 5000], prev_status="零申报")
    predicted, _, rule, _ = _empirical_merge_rule(c)
    assert (predicted, rule) == ("double", "history_double")


def test_prev_income_covers_older_month():
    c = _cand([202608, 202609], [5000, 6000], prev_income=5000)
    predicted, _, rule, reason = _empirical_merge_rule(c)
    assert predicted == "single"
    assert rule == "new_month_single"
    assert "202609" in reason


def test_prev_income_covers_all_months_skip():
    c = _cand([202608, 202609], [5000, 6000], prev_income=11000, can_skip=True)
    predicted, _, rule, _ = _empirical_merge_rule(c)
    assert (predicted, rule) == ("skip", "already_reported_skip")


def test_non_adjacent_months_prev_reported_single():
    c = _cand([202605, 202607], [5000, 7000], prev_income=1234)
    predicted, _, rule, _ = _empirical_merge_rule(c)
    assert (predicted, rule) == ("single", "gap_single")


def test_adjacent_months_no_match_prefers_double():
    c = _cand([202608, 202609], [5000, 7000], prev_income=1234)
    predicted, _, rule, _ = _empirical_merge_rule(c)
    assert (predicted, rule) == ("double", "prev_income_no_match")


def test_empirical_merge_cards_covered_single_month():
    # 上月窗口结清了 202608 批1 → 仅 202609 新增 → 单月
    c = {"cert_no": "X", "can_skip": False,
         "salary_months": [202608, 202609],
         "prev_status": "已申报", "prev_income": 10000, "prev_month": 202608,
         "slips": [
             {"salary_month": 202608, "seq": "1", "income": 9000},
             {"salary_month": 202609, "seq": "1", "income": 10000},
         ]}
    ctx = {"prev_cards_months": {"X": {(202608, "1")}}, "prev_month": 202608,
           "by_month": {202608: {"X": {"本期收入": 10000}}}}
    predicted, level, rule, _ = _empirical_merge_rule(c, ctx)
    assert predicted == "single"
    assert rule == "new_month_single"


def test_empirical_merge_cards_other_month_multi():
    # 上月窗口结清的是 202607 的钱（固定工资跨月窗口），候选 202608+202609 均未申报 → 多月
    c = {"cert_no": "Y", "can_skip": False,
         "salary_months": [202608, 202609],
         "prev_status": "已申报", "prev_income": 5100, "prev_month": 202609,
         "slips": [
             {"salary_month": 202608, "seq": "1", "income": 5100},
             {"salary_month": 202609, "seq": "1", "income": 5100},
         ]}
    ctx = {"prev_cards_months": {"Y": {(202607, "1")}}, "prev_month": 202609,
           "by_month": {202609: {"Y": {"本期收入": 5100}}}}
    predicted, level, rule, reason = _empirical_merge_rule(c, ctx)
    assert predicted == "double"
    assert rule == "new_months_double"
    assert "未申报新增" in reason


def test_empirical_merge_cards_same_month_other_batch_multi():
    # 上月窗口只结清同月其他批次 → 本次候选批次均未申报 → 多月
    c = {"cert_no": "Z", "can_skip": False,
         "salary_months": [202606, 202607],
         "prev_status": "已申报", "prev_income": 7246, "prev_month": 202606,
         "slips": [
             {"salary_month": 202606, "seq": "1", "income": 2354},
             {"salary_month": 202607, "seq": "1", "income": 2480},
         ]}
    ctx = {"prev_cards_months": {"Z": {(202606, "2")}}, "prev_month": 202606}
    predicted, level, rule, reason = _empirical_merge_rule(c, ctx)
    assert predicted == "double"
    assert rule == "new_months_double"
    assert "其他批次" in reason


def test_empirical_merge_cards_empty_falls_back_to_amount():
    # 上月窗口未结清任何卡片（发放断档/脱钩）→ 金额兜底：不匹配→多月
    c = {"cert_no": "W", "can_skip": False,
         "salary_months": [202608, 202609],
         "prev_status": "已申报", "prev_income": 99999, "prev_month": 202608,
         "slips": [
             {"salary_month": 202608, "seq": "1", "income": 8000},
             {"salary_month": 202609, "seq": "1", "income": 8000},
         ]}
    ctx = {"prev_cards_months": {}, "prev_month": 202608,
           "by_month": {202608: {"W": {"本期收入": 99999}}}}
    predicted, level, rule, _ = _empirical_merge_rule(c, ctx)
    assert predicted == "double"


def test_empirical_merge_cards_empty_amount_match_single():
    # 上月窗口无卡片但上月申报金额与候选月撞车 → 金额语义成立 → 单月（魏毅型）
    c = {"cert_no": "V", "can_skip": False,
         "salary_months": [202608, 202609],
         "prev_status": "已申报", "prev_income": 50000, "prev_month": 202608,
         "slips": [
             {"salary_month": 202608, "seq": "1", "income": 50000},
             {"salary_month": 202609, "seq": "1", "income": 10000},
         ]}
    ctx = {"prev_cards_months": {}, "prev_month": 202608,
           "by_month": {202608: {"V": {"本期收入": 50000}}}}
    predicted, level, rule, _ = _empirical_merge_rule(c, ctx)
    assert predicted == "single"
    assert rule == "new_month_single"


def test_empirical_merge_year_recon_unreported_multi():
    # 年度对账：属月卡片已结清但属期无申报行 → 漏报月 → 候选命中 → 多月
    c = {"cert_no": "R", "can_skip": False,
         "salary_months": [202606, 202607],
         "prev_status": "有报", "prev_income": 10039, "prev_month": 202607,
         "slips": [
             {"salary_month": 202606, "seq": "1", "income": 2354},
             {"salary_month": 202607, "seq": "1", "income": 7685},
         ]}
    ctx = {"prev_month": 202607, "prev_cards_months": {},
           "cards_ytd": {"R": {202606: {"amount": 9600, "windows": {202606, 202607}},
                               202607: {"amount": 10039, "windows": {202607}}}},
           "by_month": {202606: {"R": {"income": 7246}},
                        202607: {"R": {"income": 10039}}}}
    predicted, level, rule, reason = _empirical_merge_rule(c, ctx)
    assert predicted == "double"
    assert rule == "year_recon_multi"
    assert "年度对账" in reason


def test_empirical_merge_year_recon_covered_no_fire():
    # 局部金额能闭合（申报行金额=结清金额）→ 不判漏报 → 走金额兜底
    c = {"cert_no": "S", "can_skip": False,
         "salary_months": [202608, 202609],
         "prev_status": "有报", "prev_income": 5000, "prev_month": 202609,
         "slips": [
             {"salary_month": 202608, "seq": "1", "income": 5000},
             {"salary_month": 202609, "seq": "1", "income": 5000},
         ]}
    ctx = {"prev_month": 202609, "prev_cards_months": {},
           "cards_ytd": {"S": {202609: {"amount": 5000, "windows": {202609}}}},
           "by_month": {202609: {"S": {"income": 5000}}}}
    predicted, level, rule, _ = _empirical_merge_rule(c, ctx)
    # 属月 202609 结清 5000 且属期 202609 行 5000 对上 → 非漏报 → 金额兜底判单月
    assert predicted == "single"
