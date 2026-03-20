import sqlite3
import time
from datetime import datetime

def get_total_users():
    conn = sqlite3.connect('users.db')
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