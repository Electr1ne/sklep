import sqlite3
from typing import Optional, List, Tuple

DB_PATH = "halloween.db"

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        points INTEGER DEFAULT 0,
        total_earned_points INTEGER DEFAULT 0,
        has_spun INTEGER DEFAULT 0,
        is_bear1_winner INTEGER DEFAULT 0,
        rune_1_unlocked INTEGER DEFAULT 0,
        rune_2_unlocked INTEGER DEFAULT 0,
        rune_3_unlocked INTEGER DEFAULT 0,
        rune_4_unlocked INTEGER DEFAULT 0,
        safe_attempts INTEGER DEFAULT 5,
        safe_banned_until INTEGER DEFAULT 0,
        is_bear3_winner INTEGER DEFAULT 0
    );
    """)

    # Миграция колонок на случай обновления старой БД
    cols_to_add = [
        ("total_earned_points", "INTEGER DEFAULT 0"),
        ("safe_attempts", "INTEGER DEFAULT 5"),
        ("safe_banned_until", "INTEGER DEFAULT 0")
    ]
    for col_name, col_def in cols_to_add:
        try:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def};")
        except sqlite3.OperationalError:
            pass

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS promo_codes (
        code TEXT PRIMARY KEY,
        points INTEGER NOT NULL,
        is_used INTEGER DEFAULT 0,
        used_by INTEGER
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_hints (
        user_id INTEGER,
        hint_number INTEGER,
        PRIMARY KEY (user_id, hint_number)
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS event_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        is_active INTEGER DEFAULT 0,
        start_time_msk TEXT DEFAULT NULL,
        end_time_msk TEXT DEFAULT NULL,
        auto_channel_post INTEGER DEFAULT 1,
        channel_id INTEGER DEFAULT NULL,
        channel_title TEXT DEFAULT NULL,
        custom_start_post TEXT DEFAULT NULL,
        custom_finish_post TEXT DEFAULT NULL,
        bear1_claimed INTEGER DEFAULT 0,
        bear1_winner_id INTEGER DEFAULT NULL,
        total_roulette_spins INTEGER DEFAULT 0,
        roulette_start_ts INTEGER DEFAULT 0,
        safe_opened INTEGER DEFAULT 0,
        safe_attempts_limit INTEGER DEFAULT 5,
        safe_ban_duration_min INTEGER DEFAULT 30,
        safe_mode TEXT DEFAULT 'individual',
        global_safe_attempts INTEGER DEFAULT 5,
        global_safe_banned_until INTEGER DEFAULT 0,
        rune_1_name TEXT DEFAULT 'Крови 🩸',
        rune_2_name TEXT DEFAULT 'Праха 💀',
        rune_3_name TEXT DEFAULT 'Тьмы 👁️',
        rune_4_name TEXT DEFAULT 'Тыквы 🎃',
        rune_1_key TEXT DEFAULT 'SANGUIS-MORTIS-IX',
        rune_2_key TEXT DEFAULT 'CINIS-CALVARIA-77',
        rune_3_key TEXT DEFAULT 'TENEBRAE-NOCTIS-404',
        rune_4_key TEXT DEFAULT 'CUCURBITA-HEX-13',
        hint_1_text TEXT DEFAULT 'Гримуар Крови гласит: Ключ — SANGUIS-MORTIS-IX',
        hint_1_cost INTEGER DEFAULT 100,
        hint_2_text TEXT DEFAULT 'Гримуар Праха гласит: Ключ — CINIS-CALVARIA-77',
        hint_2_cost INTEGER DEFAULT 200,
        hint_3_text TEXT DEFAULT 'Гримуар Тьмы гласит: Ключ — TENEBRAE-NOCTIS-404',
        hint_3_cost INTEGER DEFAULT 350,
        hint_4_text TEXT DEFAULT 'Гримуар Тыквы гласит: Ключ — CUCURBITA-HEX-13',
        hint_4_cost INTEGER DEFAULT 500,
        bear3_claimed INTEGER DEFAULT 0,
        bear3_winner_id INTEGER DEFAULT NULL
    );
    """)

    # Миграция колонок event_config
    config_cols = [
        ("safe_attempts_limit", "INTEGER DEFAULT 5"),
        ("safe_ban_duration_min", "INTEGER DEFAULT 30"),
        ("safe_mode", "TEXT DEFAULT 'individual'"),
        ("global_safe_attempts", "INTEGER DEFAULT 5"),
        ("global_safe_banned_until", "INTEGER DEFAULT 0"),
        ("total_roulette_spins", "INTEGER DEFAULT 0"),
        ("roulette_start_ts", "INTEGER DEFAULT 0")
    ]
    for col_name, col_def in config_cols:
        try:
            cursor.execute(f"ALTER TABLE event_config ADD COLUMN {col_name} {col_def};")
        except sqlite3.OperationalError:
            pass

    cursor.execute("INSERT OR IGNORE INTO event_config (id, is_active) VALUES (1, 0);")
    conn.commit()
    conn.close()

# --- Пользователи ---

def register_user(user_id: int, username: Optional[str]):
    conn = get_connection()
    cursor = conn.cursor()
    cfg = get_event_config()
    def_attempts = cfg["safe_attempts_limit"] if cfg else 5
    cursor.execute("""
    INSERT INTO users (user_id, username, safe_attempts) VALUES (?, ?, ?)
    ON CONFLICT(user_id) DO UPDATE SET username = excluded.username;
    """, (user_id, username or "Странник", def_attempts))
    conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def update_points(user_id: int, points_delta: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (points_delta, user_id))
    conn.commit()
    conn.close()

def record_spin_attempt(now_ts: int):
    """Увеличивает общий счетчик сделанных круток для прогрессивного шанса."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE event_config SET 
        total_roulette_spins = total_roulette_spins + 1,
        roulette_start_ts = CASE WHEN roulette_start_ts = 0 THEN ? ELSE roulette_start_ts END
    WHERE id = 1;
    """, (now_ts,))
    conn.commit()
    conn.close()

def set_spin_done(user_id: int, won_bear: bool = False):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET has_spun = 1, is_bear1_winner = ? WHERE user_id = ?",
                   (1 if won_bear else 0, user_id))
    if won_bear:
        cursor.execute("UPDATE event_config SET bear1_claimed = 1, bear1_winner_id = ? WHERE id = 1", (user_id,))
    conn.commit()
    conn.close()

# --- Конфигурация ивента и посты ---

def get_event_config():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM event_config WHERE id = 1")
    row = cursor.fetchone()
    conn.close()
    return row

def set_event_status(active: bool):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE event_config SET is_active = ? WHERE id = 1", (1 if active else 0,))
    conn.commit()
    conn.close()

def set_safe_status(opened: bool):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE event_config SET safe_opened = ? WHERE id = 1", (1 if opened else 0,))
    conn.commit()
    conn.close()

def update_safe_attempts_policy(mode: str, attempts: int, ban_min: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE event_config SET 
        safe_mode = ?, 
        safe_attempts_limit = ?, 
        safe_ban_duration_min = ?,
        global_safe_attempts = ?
    WHERE id = 1;
    """, (mode, attempts, ban_min, attempts))
    cursor.execute("UPDATE users SET safe_attempts = ?", (attempts,))
    conn.commit()
    conn.close()

def toggle_auto_post() -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE event_config SET auto_channel_post = CASE WHEN auto_channel_post = 1 THEN 0 ELSE 1 END WHERE id = 1")
    conn.commit()
    cursor.execute("SELECT auto_channel_post FROM event_config WHERE id = 1")
    val = cursor.fetchone()[0]
    conn.close()
    return val

def save_bound_channel(channel_id: int, channel_title: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE event_config SET channel_id = ?, channel_title = ? WHERE id = 1",
                   (channel_id, channel_title))
    conn.commit()
    conn.close()

def set_msk_dates(start_msk: Optional[str], end_msk: Optional[str]):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE event_config SET start_time_msk = ?, end_time_msk = ? WHERE id = 1", (start_msk, end_msk))
    conn.commit()
    conn.close()

def set_custom_post(post_type: str, text: Optional[str]):
    conn = get_connection()
    cursor = conn.cursor()
    col = "custom_start_post" if post_type == "start" else "custom_finish_post"
    cursor.execute(f"UPDATE event_config SET {col} = ? WHERE id = 1", (text,))
    conn.commit()
    conn.close()

def update_safe_runes_full(names: list, keys: list, hints: list):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE event_config SET 
        rune_1_name = ?, rune_2_name = ?, rune_3_name = ?, rune_4_name = ?,
        rune_1_key = ?, rune_2_key = ?, rune_3_key = ?, rune_4_key = ?,
        hint_1_text = ?, hint_1_cost = ?,
        hint_2_text = ?, hint_2_cost = ?,
        hint_3_text = ?, hint_3_cost = ?,
        hint_4_text = ?, hint_4_cost = ?
    WHERE id = 1;
    """, (
        names[0].strip(), names[1].strip(), names[2].strip(), names[3].strip(),
        keys[0].strip().upper(), keys[1].strip().upper(), keys[2].strip().upper(), keys[3].strip().upper(),
        hints[0][0], hints[0][1],
        hints[1][0], hints[1][1],
        hints[2][0], hints[2][1],
        hints[3][0], hints[3][1]
    ))
    conn.commit()
    conn.close()

# --- Промокоды ---

def add_promo_code(code: str, points: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO promo_codes (code, points, is_used) VALUES (?, ?, 0)",
                   (code.strip().upper(), points))
    conn.commit()
    conn.close()

def claim_promo_code(code: str, user_id: int) -> Tuple[bool, str, int]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM promo_codes WHERE code = ?", (code.strip().upper(),))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, "not_found", 0
    if row["is_used"] == 1:
        conn.close()
        return False, "already_used", 0

    pts = row["points"]
    cursor.execute("UPDATE promo_codes SET is_used = 1, used_by = ? WHERE code = ?", (user_id, code.strip().upper()))
    cursor.execute("UPDATE users SET points = points + ?, total_earned_points = total_earned_points + ? WHERE user_id = ?", (pts, pts, user_id))
    conn.commit()
    conn.close()
    return True, "success", pts

def get_all_codes():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM promo_codes ORDER BY is_used ASC")
    rows = cursor.fetchall()
    conn.close()
    return rows

# --- Руны Сейфа и Блокировки ---

def unlock_user_rune(user_id: int, rune_index: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE users SET rune_{rune_index}_unlocked = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def check_all_runes_unlocked(user_id: int) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT rune_1_unlocked, rune_2_unlocked, rune_3_unlocked, rune_4_unlocked FROM users WHERE user_id = ?",
        (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return all(row[f"rune_{i}_unlocked"] == 1 for i in range(1, 5))
    return False

def get_user_hints(user_id: int) -> List[int]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT hint_number FROM user_hints WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [r["hint_number"] for r in rows]

def buy_hint(user_id: int, hint_no: int, cost: int) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    if not user or user["points"] < cost:
        conn.close()
        return False

    cursor.execute("UPDATE users SET points = points - ? WHERE user_id = ?", (cost, user_id))
    cursor.execute("INSERT OR IGNORE INTO user_hints (user_id, hint_number) VALUES (?, ?)", (user_id, hint_no))
    conn.commit()
    conn.close()
    return True

def set_safe_winner(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_bear3_winner = 1 WHERE user_id = ?", (user_id,))
    cursor.execute("UPDATE event_config SET bear3_claimed = 1, bear3_winner_id = ? WHERE id = 1", (user_id,))
    conn.commit()
    conn.close()

def deduct_safe_attempt_poly(user_id: int, ban_timestamp: int = 0) -> Tuple[int, bool]:
    conn = get_connection()
    cursor = conn.cursor()
    cfg = get_event_config()
    is_global = (cfg["safe_mode"] == "global")

    if is_global:
        cursor.execute("SELECT global_safe_attempts FROM event_config WHERE id = 1")
        cur_att = cursor.fetchone()["global_safe_attempts"]
        new_att = cur_att - 1
        if new_att <= 0:
            cursor.execute("UPDATE event_config SET global_safe_attempts = ?, global_safe_banned_until = ? WHERE id = 1",
                           (cfg["safe_attempts_limit"], ban_timestamp))
            conn.commit()
            conn.close()
            return 0, True
        else:
            cursor.execute("UPDATE event_config SET global_safe_attempts = ? WHERE id = 1", (new_att,))
            conn.commit()
            conn.close()
            return new_att, False
    else:
        cursor.execute("SELECT safe_attempts FROM users WHERE user_id = ?", (user_id,))
        cur_att = cursor.fetchone()["safe_attempts"]
        new_att = cur_att - 1
        if new_att <= 0:
            cursor.execute("UPDATE users SET safe_attempts = ?, safe_banned_until = ? WHERE user_id = ?",
                           (cfg["safe_attempts_limit"], ban_timestamp, user_id))
            conn.commit()
            conn.close()
            return 0, True
        else:
            cursor.execute("UPDATE users SET safe_attempts = ? WHERE user_id = ?", (new_att, user_id))
            conn.commit()
            conn.close()
            return new_att, False

def get_safe_status_for_user(user_id: int, current_ts: int) -> Tuple[bool, int, int]:
    cfg = get_event_config()
    user = get_user(user_id)
    is_global = (cfg["safe_mode"] == "global")

    if is_global:
        ban_until = cfg["global_safe_banned_until"]
        attempts = cfg["global_safe_attempts"]
    else:
        ban_until = user["safe_banned_until"]
        attempts = user["safe_attempts"]

    if ban_until > current_ts:
        return True, ban_until - current_ts, attempts
    return False, 0, attempts

def get_leaderboard(limit: int = 10):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT user_id, username, total_earned_points, points 
    FROM users 
    WHERE is_bear1_winner = 0 AND is_bear3_winner = 0
    ORDER BY total_earned_points DESC 
    LIMIT ?;
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows