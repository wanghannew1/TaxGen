"""test_filter_empty_cert.py - 身份证号为空数据过滤开关测试

覆盖三层:
1. config_db 开关读写 (SQLite app_config KV, 默认开启, 可持久化);
2. queries 取数函数 filter_empty_cert 参数行为 (开关透传注入 SQL);
3. app 层 /api/filter-empty-cert GET/POST 接口。
"""
import pytest

from config_db import get_filter_empty_cert, set_filter_empty_cert, get_app_config


@pytest.fixture(autouse=True)
def restore_filter_empty_cert_config():
    """测试后恢复原开关值, 避免污染真实配置。"""
    orig = get_filter_empty_cert()
    yield
    set_filter_empty_cert(orig)


class TestConfigDbSwitch:
    def test_default_enabled(self):
        """默认开启: 'filter_empty_cert' 无配置时 get_filter_empty_cert() 返回 True。"""
        assert get_filter_empty_cert() is True

    def test_set_then_get(self):
        """持久化写入后能读回 ('1'/True、'0'/False)。"""
        set_filter_empty_cert(False)
        assert get_filter_empty_cert() is False
        assert get_app_config("filter_empty_cert") == "0"
        set_filter_empty_cert(True)
        assert get_filter_empty_cert() is True
        assert get_app_config("filter_empty_cert") == "1"

    def test_set_idempotent(self):
        """重复设置不产生异常, 值稳定。"""
        set_filter_empty_cert(True)
        set_filter_empty_cert(True)
        assert get_filter_empty_cert() is True


class TestQueriesFilterParam:
    def test_sql_contains_filter_when_enabled(self, conn, monkeypatch):
        """开关开启时 get_salary_records 的 SQL 含 AND ac01.AAC002 IS NOT NULL。

        不连接真实执行 (monkeypatch cursor), 仅验证 filter_empty_cert 参数
        正确控制 SQL 文本 (开启=带过滤条件, 关闭=不带)。
        """
        from queries import get_salary_records
        captured = {}

        class FakeCursor:
            def execute(self, sql, params):
                captured["sql"] = sql
                captured["params"] = params

            def fetchall(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class FakeConn:
            def cursor(self):
                return FakeCursor()

        monkeypatch.setattr("config_db.get_filter_empty_cert", lambda: True)
        get_salary_records(FakeConn(), 202608)
        assert "AAC002 IS NOT NULL" in captured["sql"].upper()

        monkeypatch.setattr("config_db.get_filter_empty_cert", lambda: False)
        get_salary_records(FakeConn(), 202608)
        assert "AAC002 IS NOT NULL" not in captured["sql"].upper()

    def test_by_combos_sql_filter(self, monkeypatch):
        """get_salary_records_by_combos 同样受开关控制。"""
        from queries import get_salary_records_by_combos
        captured = {}

        class FakeCursor:
            def execute(self, sql, params):
                captured["sql"] = sql
                captured["params"] = params

            def fetchall(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class FakeConn:
            def cursor(self):
                return FakeCursor()

        monkeypatch.setattr("config_db.get_filter_empty_cert", lambda: True)
        get_salary_records_by_combos(FakeConn(), [{"unit": 37725, "salary_month": 202608, "seq": "1"}])
        assert "AAC002 IS NOT NULL" in captured["sql"].upper()

        monkeypatch.setattr("config_db.get_filter_empty_cert", lambda: False)
        get_salary_records_by_combos(FakeConn(), [{"unit": 37725, "salary_month": 202608, "seq": "1"}])
        assert "AAC002 IS NOT NULL" not in captured["sql"].upper()


class TestAppApi:
    def test_api_get_default(self, app_client):
        """GET /api/filter-empty-cert 返回开关状态。"""
        resp = app_client.get("/api/filter-empty-cert")
        assert resp.status_code == 200
        assert "enabled" in resp.get_json()

    def test_api_set_and_get(self, app_client):
        """POST 关闭 → GET 读到 False; POST 开启 → GET 读到 True。"""
        resp = app_client.post("/api/filter-empty-cert", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.get_json()["enabled"] is False
        assert app_client.get("/api/filter-empty-cert").get_json()["enabled"] is False

        resp = app_client.post("/api/filter-empty-cert", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.get_json()["enabled"] is True
        assert app_client.get("/api/filter-empty-cert").get_json()["enabled"] is True