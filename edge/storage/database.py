import sqlite3
import json
import time
import threading
from pathlib import Path

class Database:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._local = threading.local()
        self._init_schema()

    def _conn(self):
        if not hasattr(self._local, 'conn'):
            self._local.conn = sqlite3.connect(self._path)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_schema(self):
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sensor_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id   TEXT    NOT NULL,
                timestamp   REAL    NOT NULL,
                data        TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sensor_log_ts
                ON sensor_log(timestamp DESC);

            CREATE TABLE IF NOT EXISTS event_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   REAL    NOT NULL,
                event_type  TEXT    NOT NULL,
                source      TEXT,
                detail      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_event_log_ts
                ON event_log(timestamp DESC);

            CREATE TABLE IF NOT EXISTS decision_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   REAL    NOT NULL,
                trigger     TEXT,
                source      TEXT,
                actions     TEXT,
                feedback    TEXT
            );
        """)
        conn.commit()

    def log_sensor(self, device_id: str, timestamp: float, data: dict):
        self._conn().execute(
            "INSERT INTO sensor_log(device_id,timestamp,data) VALUES(?,?,?)",
            (device_id, timestamp, json.dumps(data))
        )
        self._conn().commit()

    def log_event(self, event_type: str, source: str, detail: dict):
        self._conn().execute(
            "INSERT INTO event_log(timestamp,event_type,source,detail) VALUES(?,?,?,?)",
            (time.time(), event_type, source, json.dumps(detail))
        )
        self._conn().commit()

    def log_decision(self, trigger: str, source: str,
                     actions: list, feedback: str):
        self._conn().execute(
            "INSERT INTO decision_log(timestamp,trigger,source,actions,feedback)"
            " VALUES(?,?,?,?,?)",
            (time.time(), trigger, source,
             json.dumps(actions), feedback)
        )
        self._conn().commit()

    def cleanup_old(self, keep_days: int = 7):
        """定期清理旧数据，控制数据库体积"""
        cutoff = time.time() - keep_days * 86400
        conn = self._conn()
        conn.execute("DELETE FROM sensor_log WHERE timestamp < ?", (cutoff,))
        conn.execute("DELETE FROM event_log  WHERE timestamp < ?", (cutoff,))
        conn.commit()