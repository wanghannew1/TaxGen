"""compare_personnel 纯逻辑单元测试 (无需 Oracle 连接)"""
from datetime import datetime

from templates_gen.personnel_compare import compare_personnel


def _persons(specs):
    """specs: [(证件号, 离职日期为空串表示在职]] → 个税端人员 dict 列表"""
    return [{"证件号码": c, "姓名": f"人{c}", "离职日期": dep} for c, dep in specs]


class TestStatsFields:
    """测试 compare_personnel 返回的 5 个新统计字段"""

    def test_stats_fields_present_and_correct(self):
        tax = _persons([
            ("A001", ""), ("B002", ""), ("C003", ""),
        ])
        _, _, _, stats = compare_personnel(
            tax, payroll_certs=set(), payroll_personnel=[],
            salary_end_dates={},
        )
        assert "active_total" in stats
        assert "unpaid_total" in stats
        assert "contract_total" in stats
        assert "filtered_active_count" in stats
        assert "filtered_payroll_count" in stats
        assert stats["active_total"] == 3
        assert stats["unpaid_total"] == 0
        assert stats["contract_total"] == 0

    def test_no_filter_invariant(self):
        tax = _persons([("A001", ""), ("B002", "")])
        _, _, _, stats = compare_personnel(
            tax, payroll_certs={"A001", "B002"},
            payroll_personnel=[], salary_end_dates={"A001": None, "B002": None},
        )
        assert stats["filtered_active_count"] == stats["active_total"]
        assert stats["filtered_payroll_count"] == stats["payroll_total"]

    def test_filtered_counts_with_filter(self):
        tax = _persons([("A001", ""), ("B002", ""), ("C003", "")])
        person_units = {
            "A001": {"pay_handlers": ["E001"], "unit_code": 1, "unit_name": "单位A", "dept_name": "部门X"},
            "B002": {"pay_handlers": ["E001"], "unit_code": 2, "unit_name": "单位B", "dept_name": "部门X"},
            "C003": {"pay_handlers": ["E002"], "unit_code": 1, "unit_name": "单位A", "dept_name": "部门Y"},
        }
        _, _, _, stats = compare_personnel(
            tax, payroll_certs={"A001", "B002", "C003"},
            payroll_personnel=[], salary_end_dates={"A001": None, "B002": None, "C003": None},
            person_units=person_units, filter_units=[1],
        )
        assert stats["filtered_active_count"] == 2
        assert stats["filtered_active_count"] <= stats["active_total"]
        assert stats["filtered_payroll_count"] == 2
        assert stats["filtered_payroll_count"] <= stats["payroll_total"]

    def test_payroll_cert_not_in_active_still_counted_in_payroll_filtered(self):
        tax = _persons([("A001", ""), ("B002", "2026-06-30")])
        person_units = {
            "B002": {"pay_handlers": ["E001"], "unit_code": 1, "unit_name": "单位A", "dept_name": "部门X"},
        }
        _, _, _, stats = compare_personnel(
            tax, payroll_certs={"A001", "B002"},
            payroll_personnel=[], salary_end_dates={"A001": None, "B002": None},
            person_units=person_units,
        )
        # filtered_payroll_count 计入 B002 (在 payroll_certs 且通过 _passes_filter)
        assert stats["filtered_payroll_count"] == 2  # A001 + B002
        # filtered_active_count 不计入 B002 (B002 有离职日期，不在 active_certs)
        assert stats["filtered_active_count"] == 1  # 只有 A001


class TestDeferredPayZeroDeclare:
    """延期发放人员 (次月月初已发) → 零申报, 而非待确认零申报"""

    def _run(self, deferred=None):
        tax = _persons([
            ("A001", ""),   # 有工资结束年月 → 近期离职
            ("B002", ""),   # 无结束年月、无发放 → 待确认
            ("C003", ""),   # 无结束年月, 但延期发放 → 零申报
            ("D004", ""),   # 申报期有发薪 → 不进零申报
        ])
        salary_end = {"A001": datetime(2026, 8, 31)}
        return compare_personnel(
            tax,
            payroll_certs={"D004"},
            payroll_personnel=[],
            salary_end_dates=salary_end,
            payroll_start_certs={"D004"},
            deferred_pay_persons=deferred,
        )

    def test_deferred_persons_moved_from_pending_to_zero(self):
        add_rows, departed_rows, pending_rows, stats = self._run(deferred={"C003"})
        assert stats["departed_count"] == 1
        assert stats["pending_count"] == 1
        # 延期发放人员 C003 进主零申报, 不进待确认零申报
        assert "C003" in stats["zero_certs"]
        assert "C003" not in stats["zero_pending_certs"]
        # 待确认人员 B002 仍进待确认零申报
        assert "B002" in stats["zero_pending_certs"]
        assert "B002" not in stats["zero_certs"]
        # 申报期有发薪的 D004 既不零申报也不待确认
        assert "D004" not in stats["zero_certs"]
        assert "D004" not in stats["zero_pending_certs"]

    def test_without_deferred_param_keeps_pending(self):
        # 未启用延期发放判定 (旧行为): C003 应留在待确认
        _, _, _, stats = self._run(deferred=None)
        assert stats["pending_count"] == 2
        assert "C003" in stats["zero_pending_certs"]
        assert "C003" not in stats["zero_certs"]

    def test_deferred_with_salary_end_still_departed(self):
        # 延期发放人员若已有工资结束年月 → 仍按近期离职, 不因延期发放豁免
        tax = _persons([
            ("E005", ""),
        ])
        _, departed_rows, _, stats = compare_personnel(
            tax,
            payroll_certs=set(),
            payroll_personnel=[],
            salary_end_dates={"E005": datetime(2026, 8, 31)},
            payroll_start_certs=set(),
            deferred_pay_persons={"E005"},
        )
        assert stats["departed_count"] == 1
        assert "E005" not in stats["zero_certs"]
        assert len(departed_rows) == 1