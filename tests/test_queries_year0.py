"""get_person_tc90_info 脏数据防护: ATC90AV 异常值 → salary_end_ym=0 不崩溃 (纯逻辑, 无需 Oracle)"""
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
        self._rows = rows

    def cursor(self):
        return FakeCursor(self._rows)


@pytest.fixture(autouse=True)
def _clear_tc90_cache():
    # 每用例重置模块级 5 分钟 TTL 缓存, 防止跨用例命中污染断言
    queries._TC90_INFO_CACHE = {"ts": 0.0, "data": None}
    yield
    queries._TC90_INFO_CACHE = {"ts": 0.0, "data": None}


def _tc90_rows(*rows):
    # 行结构: (AAC002, ATB930, ATC90X, AAB004, AAE019, ATC90AV, ATC90C)
    return list(rows)


class TestPersonTc90Info:
    def test_clean_salary_end_ym(self):
        conn = FakeConn(_tc90_rows(
            ("C1", 100, "单元100", "单位甲", "经办A", 202605, 20200101),
        ))
        info = queries.get_person_tc90_info(conn)
        assert info["C1"]["salary_end_ym"] == 202605
        assert info["C1"]["unit_code"] == 100
        assert info["C1"]["contract_handlers"] == ["经办A"]

    def test_dirty_av_year_zero_no_crash(self):
        # 2026-09-11 实测脏数据: ATC90AV=6 → divmod → y=0, 曾经 datetime(year=0) 崩溃
        conn = FakeConn(_tc90_rows(
            ("C1", 100, "单元100", "单位甲", "", 6, 20200101),
        ))
        info = queries.get_person_tc90_info(conn)
        assert info["C1"]["salary_end_ym"] == 0

    def test_dirty_av_year_below_1900_no_crash(self):
        conn = FakeConn(_tc90_rows(
            ("C1", 100, "单元100", "单位甲", "", 1206, 20200101),
        ))
        info = queries.get_person_tc90_info(conn)
        assert info["C1"]["salary_end_ym"] == 0

    def test_dirty_av_month_out_of_range_no_crash(self):
        conn = FakeConn(_tc90_rows(
            ("C1", 100, "单元100", "单位甲", "", 202613, 20200101),
        ))
        info = queries.get_person_tc90_info(conn)
        assert info["C1"]["salary_end_ym"] == 0

    def test_dirty_av_ignored_others_valid(self):
        # 同一合同 (ATC90C 相同) 多行: 脏值行不参与 MAX, 正常行取值不受污染
        conn = FakeConn(_tc90_rows(
            ("C1", 100, "单元100", "单位甲", "", 6, 20200101),
            ("C1", 100, "单元100", "单位甲", "", 202605, 20200101),
        ))
        info = queries.get_person_tc90_info(conn)
        assert info["C1"]["salary_end_ym"] == 202605

    def test_last_contract_wins_by_atc90c(self):
        # 最后一份合同 (ATC90C 最大) 决定单位/经办人/工资结束
        conn = FakeConn(_tc90_rows(
            ("C1", 100, "单元100", "单位甲", "旧经办", 202601, 20200101),
            ("C1", 200, "单元200", "单位乙", "新经办", 202605, 20210101),
        ))
        info = queries.get_person_tc90_info(conn)
        assert info["C1"]["unit_code"] == 200
        assert info["C1"]["unit_name"] == "单元200"
        assert info["C1"]["contract_handlers"] == ["新经办"]
        assert info["C1"]["salary_end_ym"] == 202605

    def test_cert_normalized_to_upper(self):
        conn = FakeConn(_tc90_rows(
            ("c1", 100, "单元100", "单位甲", "", 202605, 20200101),
        ))
        info = queries.get_person_tc90_info(conn)
        assert "C1" in info and "c1" not in info

    def test_handlers_deduped(self):
        conn = FakeConn(_tc90_rows(
            ("C1", 100, "单元100", "单位甲", "经办A", 202605, 20200101),
            ("C1", 100, "单元100", "单位甲", "经办A", 202605, 20200102),
        ))
        info = queries.get_person_tc90_info(conn)
        assert info["C1"]["contract_handlers"] == ["经办A"]

    def test_result_cached_not_reconsulted(self):
        # 5 分钟 TTL 缓存: 二次调用不再查库 (FakeConn 无第二次 fetch)
        rows = _tc90_rows(("C1", 100, "单元100", "单位甲", "", 202605, 20200101))
        conn = FakeConn(rows)
        first = queries.get_person_tc90_info(conn)
        second = queries.get_person_tc90_info(conn)
        assert first is second