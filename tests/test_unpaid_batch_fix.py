"""get_unpaid_salary_cert_months 批次传染修复测试 (纯逻辑, 无需 Oracle)。

2026-09-14 bug (平高华生 39474 整组丢失): 原 SQL 逐 TC93 批次 LEFT JOIN TC8M,
同月多批次 (批1已发 + 批2零薪未发, 同一批人) 时批2 未发状态传染批1 已发记录,
filter_zero_records 据此整组剔除。修复: "全部已做记录 MINUS 至少一批已发记录"。
"""
import pytest

import queries


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    """覆盖 conftest 的 autouse setup_db: 纯逻辑测试不建 Oracle 连接池"""
    yield


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.sql = None
        self.binds = None

    def execute(self, sql, binds=None):
        self.sql = sql
        self.binds = binds
        return self

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, rows):
        self._cursor = FakeCursor(rows)

    def cursor(self):
        return self._cursor


class TestUnpaidBatchSemantics:
    def test_sql_uses_minus_not_per_batch_left_join(self):
        """新口径必须用 MINUS (全部 MINUS 至少一批已发), 不得逐批次判未发。"""
        conn = FakeConn([("C1", 202608)])
        result = queries.get_unpaid_salary_cert_months(conn, [202608])
        sql = conn.cursor().sql
        assert sql.count("MINUS") == 1
        # 原逐批次 LEFT JOIN 口径已移除: 未发判定不再以单批次无已发为准
        assert "LEFT JOIN TC8M" not in sql
        assert result == {("C1", 202608)}

    def test_batch_and_in_chunks(self):
        """多月份分批 (IN 上限 _IN_BATCH_SIZE) 仍走两次全量 + MINUS。"""
        months = list(range(202601, 202632))
        conn = FakeConn([("C1", 202601)])
        queries.get_unpaid_salary_cert_months(conn, months)
        assert conn.cursor().sql.count("MINUS") == 1
        assert ":m0" in conn.cursor().sql

    def test_persons_variant_same_minus_semantics(self):
        """get_unpaid_salary_persons 与 cert_months 同口径 (也走 MINUS)。"""
        conn = FakeConn([("C1",)])
        result = queries.get_unpaid_salary_persons(conn, [202608])
        sql = conn.cursor().sql
        assert sql.count("MINUS") == 1
        assert "LEFT JOIN TC8M" not in sql
        assert result == {"C1"}

    def test_empty_months_no_query(self):
        conn = FakeConn([])
        assert queries.get_unpaid_salary_cert_months(conn, []) == set()
        assert queries.get_unpaid_salary_persons(conn, []) == set()