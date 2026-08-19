import os
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    'host': os.getenv('DB_HOST', '10.0.0.8'),
    'port': int(os.getenv('DB_PORT', '1521')),
    'service_name': os.getenv('DB_SERVICE_NAME', 'orcl'),
    'user': os.getenv('DB_USER', 'ccrcpq'),
    'password': os.getenv('DB_PASSWORD', '')
}
