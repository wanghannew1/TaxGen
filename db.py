import oracledb
from config import DB_CONFIG

# Oracle Instant Client location (needed for thick mode; required because the
# target database is Oracle 11g, which thin mode does not support - DPY-3010)
ORACLE_CLIENT_LIB_DIR = '/opt/oracle/instantclient_23_4'

_pool = None


def init_db():
    global _pool
    oracledb.init_oracle_client(lib_dir=ORACLE_CLIENT_LIB_DIR)
    dsn = f"{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['service_name']}"
    _pool = oracledb.create_pool(
        user=DB_CONFIG['user'],
        password=DB_CONFIG['password'],
        dsn=dsn,
        min=1,
        max=10,
        disable_oob=True,
        tcp_connect_timeout=10,
    )
    print(f"Database pool created: {dsn}")
    _ensure_special_unit_table()


def _ensure_special_unit_table():
    """确保特殊结算单元配置表存在 (工资为0不增员的结算单元)。"""
    try:
        conn = _pool.acquire()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE special_unit_config (
                        unit_code NUMBER(10) PRIMARY KEY,
                        unit_name VARCHAR2(200),
                        zero_salary_no_add NUMBER(1) DEFAULT 1,
                        created_at TIMESTAMP DEFAULT SYSTIMESTAMP
                    )
                    """
                )
        except oracledb.DatabaseError as e:
            # ORA-00955: name is already used by an existing object → 表已存在
            if "ORA-00955" not in str(e):
                raise
        finally:
            _pool.release(conn)
    except Exception:
        pass


def get_connection():
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_db() first.")
    return _pool.acquire()


def close_db():
    global _pool
    if _pool:
        _pool.close()
        _pool = None
        print("Database pool closed")
