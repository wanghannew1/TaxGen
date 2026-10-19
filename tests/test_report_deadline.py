"""test_report_deadline.py - 报税截止日可配置化测试

覆盖三层:
1. config_db 截止日读写 (SQLite app_config KV, 默认 15, 越界校验, 非法值回退默认);
2. queries.get_default_report_month 规则 (截止日前→上月, 次日起→当月,
   无发薪记录回退最近发薪月);
3. app 层 /api/report-deadline GET/POST 接口。
"""
import pytest

from config_db import (
    get_report_deadline_day,
    set_report_deadline_day,
    get_app_config,
    set_app_config,
)


@pytest.fixture(autouse=True)
def restore_report_deadline():
    """测试后恢复原截止日, 避免污染真实配置。"""
    orig = get_app_config("report_deadline_day", "")
    yield
    if orig == "":
        import config_db
        conn = config_db.get_db()
        conn.execute("DELETE FROM app_config WHERE key = 'report_deadline_day'")
        conn.commit()
        conn.close()
    else:
        set_app_config("report_deadline_day", orig)


class TestConfigDbDeadline:
    def test_default_15(self):
        """未配置时 get_report_deadline_day() 返回 15。"""
        import config_db
        conn = config_db.get_db()
        conn.execute("DELETE FROM app_config WHERE key = 'report_deadline_day'")
        conn.commit()
        conn.close()
        assert get_report_deadline_day() == 15

    def test_set_then_get(self):
        """持久化写入后能读回。"""
        set_report_deadline_day(20)
        assert get_report_deadline_day() == 20
        assert get_app_config("report_deadline_day") == "20"

    def test_set_rejects_out_of_range(self):
        """0 / 32 / 负数均抛 ValueError。"""
        for bad in (0, 32, -1, 100):
            with pytest.raises(ValueError):
                set_report_deadline_day(bad)

    def test_invalid_stored_value_falls_back(self):
        """库中存了非法值时回退默认 15, 不抛异常。"""
        set_app_config("report_deadline_day", "abc")
        assert get_report_deadline_day() == 15
        set_app_config("report_deadline_day", "99")
        assert get_report_deadline_day() == 15


class _FakeCursor:
    """只支撑 get_default_report_month 的 MAX(ATC8G7) 查询。"""

    def __init__(self, latest):
        self._latest = latest

    def execute(self, sql, params=None):
        self._sql = sql

    def fetchone(self):
        return (self._latest,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, latest):
        self._latest = latest

    def cursor(self):
        return _FakeCursor(self._latest)


class TestDefaultReportMonthRule:
    """now 注入固定时间, 不依赖真实系统时钟。"""

    def _run(self, now, deadline, latest=202610):
        from datetime import datetime
        from queries import get_default_report_month
        set_report_deadline_day(deadline)
        return get_default_report_month(_FakeConn(latest), now=datetime(*now))

    def test_before_deadline_returns_last_month(self):
        """截止日(含)当天 → 上月。"""
        # 2026-10-15, 截止日 15 → 202609
        assert self._run((2026, 10, 15), 15) == 202609

    def test_after_deadline_returns_current_month(self):
        """截止日次日 → 当月。"""
        # 2026-10-16, 截止日 15 → 202610
        assert self._run((2026, 10, 16), 15) == 202610

    def test_extended_deadline(self):
        """截止日改 20: 20 号仍报上月, 21 号起报当月。"""
        assert self._run((2026, 10, 20), 20) == 202609
        assert self._run((2026, 10, 21), 20) == 202610

    def test_early_deadline(self):
        """截止日改 10: 10 号仍报上月, 11 号起报当月。"""
        assert self._run((2026, 10, 10), 10) == 202609
        assert self._run((2026, 10, 11), 10) == 202610

    def test_year_boundary(self):
        """1 月上旬跨年 → 上年 12 月。"""
        assert self._run((2026, 1, 10), 15) == 202512

    def test_fallback_to_latest_pay_month(self):
        """默认月无发薪记录(> latest)时回退最近发薪月。"""
        # 2026-10-20 截止日 15 → 默认 202610, 但 latest 只有 202609 → 202609
        assert self._run((2026, 10, 20), 15, latest=202609) == 202609


class TestAppApi:
    def test_api_get_default(self, app_client):
        """GET /api/report-deadline 返回 day。"""
        resp = app_client.get("/api/report-deadline")
        assert resp.status_code == 200
        assert "day" in resp.get_json()

    def test_api_set_and_get(self, app_client):
        """POST 修改 → GET 读到新值。"""
        resp = app_client.post("/api/report-deadline", json={"day": 20})
        assert resp.status_code == 200
        assert resp.get_json()["day"] == 20
        assert app_client.get("/api/report-deadline").get_json()["day"] == 20

    def test_api_set_then_reset(self, app_client):
        """恢复默认 15, 避免影响其他测试。"""
        resp = app_client.post("/api/report-deadline", json={"day": 15})
        assert resp.status_code == 200
        assert resp.get_json()["day"] == 15

    def test_api_rejects_invalid(self, app_client):
        """非法值返回 400 且带 error 提示。"""
        for bad in (0, 32, "abc", None):
            resp = app_client.post("/api/report-deadline", json={"day": bad})
            assert resp.status_code == 400
            assert "error" in resp.get_json()
