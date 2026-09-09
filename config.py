import os
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    'host': os.getenv('DB_HOST', '10.0.0.8'),
    'port': int(os.getenv('DB_PORT', '1521')),
    'service_name': os.getenv('DB_SERVICE_NAME', 'orcl'),
    'user': os.getenv('DB_USER', 'ccrcpq'),
    'password': os.getenv('DB_PASSWORD', ''),
    # 连接池空闲管理: min=0 无请求时池内零连接(不常驻会话, 便于数据库维护 DROP USER)
    # timeout=300 池中空闲连接超过 300 秒自动销毁
    'pool_min': int(os.getenv('DB_POOL_MIN', '0')),
    'pool_timeout': int(os.getenv('DB_POOL_TIMEOUT', '300')),
}
