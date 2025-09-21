import sqlite3
import json
from typing import List, Dict

# Имя файла, в котором будет храниться вся наша база данных
DB_FILE = "conversation_history.db"

# Стандартный лимит истории, если пользователь не задал свой
DEFAULT_HISTORY_LIMIT = 12


def init_db():
    """
    Инициализирует базу данных.
    Создает две таблицы: `history` для сообщений и `user_settings` для настроек.
    Вызывается один раз при старте бота.
    """
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()

        # 1. Таблица для хранения истории диалогов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                parts TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. Таблица для хранения персональных настроек пользователей
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                history_limit INTEGER NOT NULL DEFAULT {}
            )
        """.format(DEFAULT_HISTORY_LIMIT))

        conn.commit()


def set_history_limit(user_id: int, limit: int):
    """
    Устанавливает или обновляет лимит истории для конкретного пользователя.
    """
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Команда INSERT OR REPLACE очень удобна:
        # она создает запись, если ее нет, или заменяет существующую.
        cursor.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, history_limit) VALUES (?, ?)",
            (user_id, limit)
        )
        conn.commit()


def get_history_limit(user_id: int) -> int:
    """
    Получает лимит истории для пользователя.
    Если для пользователя настройка не найдена, возвращает значение по умолчанию.
    """
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT history_limit FROM user_settings WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        # Если result не пустой (т.е. запись найдена), возвращаем значение. Иначе - стандартное.
        return result[0] if result else DEFAULT_HISTORY_LIMIT


def add_message_to_history(user_id: int, role: str, parts: List[Dict]):
    """
    Добавляет одно сообщение (пользователя или модели) в таблицу history.
    """
    # Gemini ожидает 'parts' в виде списка словарей,
    # но в базе данных мы можем хранить только простые типы.
    # Поэтому мы преобразуем список в строку формата JSON.
    parts_json = json.dumps(parts)
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO history (user_id, role, parts) VALUES (?, ?, ?)",
            (user_id, role, parts_json)
        )
        conn.commit()


def get_history(user_id: int) -> List[Dict]:
    """
    Извлекает историю сообщений для пользователя, используя его персональный лимит.
    Форматирует данные в список, готовый для отправки в Gemini API.
    """
    # Сначала получаем персональный лимит для этого пользователя
    limit = get_history_limit(user_id)

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Выбираем N последних сообщений для данного пользователя
        cursor.execute(
            "SELECT role, parts FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
            (user_id, limit)
        )
        rows = cursor.fetchall()

        # Результаты из базы идут в обратном порядке (от новых к старым),
        # а Gemini требует прямой порядок (от старых к новым). Переворачиваем.
        rows.reverse()

        # Преобразуем данные обратно в формат, который понимает Gemini API
        history = []
        for role, parts_json in rows:
            history.append({
                "role": role,
                "parts": json.loads(parts_json)  # Преобразуем JSON-строку обратно в список
            })
        return history


def clear_history(user_id: int):
    """
    Полностью удаляет историю диалога для указанного пользователя.
    Не затрагивает его настройки.
    """
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
        conn.commit()