import oracledb
from config import DB_CONFIG

# Oracle Instant Client location (needed for thick mode; required because the
# target database is Oracle 11g, which thin mode does not support - DPY-3010)
ORACLE_CLIENT_LIB_DIR = '/opt/oracle/instantclient_23_4'

# ⚠️ 安全规则: Oracle (工资业务库) 只允许只读访问。
# 禁止在本模块创建/修改任何 Oracle 表或数据。
# 应用自身的配置数据一律存入自建 SQLite (config_db.py → config.db)。

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
