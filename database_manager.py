import sqlite3

class DatabaseManager:
    def __init__(self, db_name="roip_client_cache.db"):
        self.db_name = db_name
        self.init_local_database()

    def get_connection(self):
        return sqlite3.connect(self.db_name)

    def init_local_database(self):
        """Inisialisasi pembuatan tabel lokal SQLite jika belum ada."""
        conn = self.get_connection()
        cursor = conn.cursor()

        # 1. Tabel Cache User (Disesuaikan dengan tambahan field jika diperlukan)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS local_users_cache (
                id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                role TEXT,
                status TEXT,
                connection_status TEXT DEFAULT 'offline',
                last_login_at TEXT,
                satker TEXT,
                latitude REAL,
                longitude REAL
            )
        ''')

        # 2. Tabel Radio Logs Lokal (Pencatatan PTT)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS radio_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                room_id INTEGER,
                audio_url TEXT,
                duration_seconds INTEGER,
                activity_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()
        conn.close()

    def save_radio_log_local(self, user_id, room_id, audio_url, duration_seconds, activity_type):
        """Menyimpan riwayat transmisi radio secara lokal."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO radio_logs (user_id, room_id, audio_url, duration_seconds, activity_type)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, room_id, audio_url, duration_seconds, activity_type))
        conn.commit()
        conn.close()