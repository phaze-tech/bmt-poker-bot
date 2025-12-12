# storage.py
import sqlite3
from pathlib import Path
from typing import List

DB_PATH = Path(__file__).parent / "bmt_poker.sqlite3"


# --------------------------------------------
# DB CONNECTION
# --------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------
# INITIAL SETUP
# --------------------------------------------
def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS players (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            total_chips_won INTEGER DEFAULT 0,
            hands_played INTEGER DEFAULT 0,
            hands_won INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()


# --------------------------------------------
# PLAYER MANAGEMENT
# --------------------------------------------
def ensure_player(user_id: int, name: str):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT user_id FROM players WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if row is None:
        cur.execute(
            "INSERT INTO players (user_id, name) VALUES (?, ?)",
            (user_id, name),
        )
    else:
        cur.execute(
            "UPDATE players SET name = ? WHERE user_id = ?",
            (name, user_id),
        )

    conn.commit()
    conn.close()


# --------------------------------------------
# RECORD HAND RESULT
# --------------------------------------------
def record_hand_result(user_id: int, chips_delta: int, won_hand: bool):
    """
    chips_delta = +X für Gewinner, 0 für Verlierer
    won_hand = True/False
    """
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        UPDATE players
        SET
            total_chips_won = total_chips_won + ?,
            hands_played = hands_played + 1,
            hands_won = hands_won + ?
        WHERE user_id = ?
    """, (chips_delta, 1 if won_hand else 0, user_id))

    conn.commit()
    conn.close()


# --------------------------------------------
# LEADERBOARD
# --------------------------------------------
def get_leaderboard(limit: int = 20) -> List[sqlite3.Row]:
    """
    returns rows sorted by chips won (DESC)
    each row = name, total_chips_won, hands_played, hands_won
    """
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT name, total_chips_won, hands_played, hands_won
        FROM players
        ORDER BY total_chips_won DESC
        LIMIT ?
    """, (limit,))

    rows = cur.fetchall()
    conn.close()
    return rows

def reset_stats():
    """
    Löscht alle Einträge aus dem Leaderboard / Player-Stats.
    Wird von /fullreset aufgerufen.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM players")
    conn.commit()
    conn.close()

# --------------------------------------------
# FULL RESET OF STATS
# --------------------------------------------
def reset_all_stats():
    """
    Setzt alle Leaderboard-Werte zurück:
    - total_chips_won = 0
    - hands_played = 0
    - hands_won = 0

    Spieler bleiben in der Tabelle erhalten, fangen aber wieder bei 0 an.
    """
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        UPDATE players
        SET
            total_chips_won = 0,
            hands_played = 0,
            hands_won = 0
    """)

    conn.commit()
    conn.close()
