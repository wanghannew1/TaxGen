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
