import libsql_experimental as libsql
import time
import os
from dotenv import load_dotenv
from datetime import datetime


TURSO_URL= os.getenv("TURSO_URL")
TURSO_AUTH_TOKEN= os.getenv("TURSO_AUTH_TOKEN")
def get_total_users():
    conn = libsql.connect(TURSO_URL, auth_token= TURSO_AUTH_TOKEN)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    conn.close()
    return total_users


while True:
    try:
        total_users = get_total_users()
        print(f"{datetime.now()}: Total users: {total_users}")

    except Exception as e:
        print(f"{datetime.now()}: Error fetching total users: {e}")
     
    time.sleep(3600)  