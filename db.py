"""SQLite 래퍼.

- 세션 단위로 원자 기록한다(누적 합계를 따로 들고 있지 않음).
- WAL 모드로 작은 쓰기에 안전 + SD카드 마모 최소화.
- 유저는 동시에 한 음성 채널에만 있으므로, 유저당 열린 세션(end_utc IS NULL)은 최대 1개.
"""

import os
import sqlite3

from timeutils import parse_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    username     TEXT,
    channel_id   INTEGER NOT NULL,
    kind         TEXT NOT NULL,          -- 'study' | 'rest'
    start_utc    TEXT NOT NULL,          -- ISO8601, tz-aware (UTC)
    end_utc      TEXT,                   -- NULL이면 진행 중
    duration_sec INTEGER                 -- 종료 시 계산
);
CREATE INDEX IF NOT EXISTS idx_sessions_open ON sessions(user_id) WHERE end_utc IS NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_utc);
"""


class Database:
    def __init__(self, path: str):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # --- 세션 열기/닫기 ---------------------------------------------------

    def open_session(self, user_id: int, username: str, channel_id: int,
                     kind: str, start_utc: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO sessions (user_id, username, channel_id, kind, start_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, channel_id, kind, start_utc),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_open_session(self, user_id: int):
        return self.conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? AND end_utc IS NULL "
            "ORDER BY start_utc DESC LIMIT 1",
            (user_id,),
        ).fetchone()

    def get_all_open_sessions(self):
        return self.conn.execute(
            "SELECT * FROM sessions WHERE end_utc IS NULL"
        ).fetchall()

    def close_session(self, session_id: int, end_utc: str):
        """세션을 end_utc로 종료하고 duration을 계산해 저장. 종료된 행을 반환."""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        duration = max(0, int(
            (parse_iso(end_utc) - parse_iso(row["start_utc"])).total_seconds()
        ))
        self.conn.execute(
            "UPDATE sessions SET end_utc = ?, duration_sec = ? WHERE id = ?",
            (end_utc, duration, session_id),
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()

    def close_open_for_user(self, user_id: int, end_utc: str):
        """유저의 열린 세션을 닫는다. 없으면 None."""
        row = self.get_open_session(user_id)
        if row is None:
            return None
        return self.close_session(row["id"], end_utc)

    # --- 조회 ------------------------------------------------------------

    def sessions_in_window(self, w_start_iso: str, w_end_iso: str,
                           user_id: int = None, kind: str = None):
        """구간과 겹칠 가능성이 있는 세션들을 반환(교집합 계산은 호출측에서).

        조건: start < 구간끝 AND (end IS NULL OR end > 구간시작)
        저장 포맷이 동일한 ISO8601(UTC, +00:00)이라 문자열 비교로 안전.
        """
        sql = "SELECT * FROM sessions WHERE start_utc < ? AND (end_utc IS NULL OR end_utc > ?)"
        params = [w_end_iso, w_start_iso]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        return self.conn.execute(sql, params).fetchall()

    def close(self):
        self.conn.close()
