import telebot
from flask import Flask, request
import sqlite3
import os
from datetime import datetime

app = Flask(__name__)

TOKEN = os.getenv('TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')          # основной канал (можно менять в Render)
ADMIN_ID = int(os.getenv('ADMIN_ID'))
SECRET = os.getenv('SECRET')

bot = telebot.TeleBot(TOKEN, threaded=False, skip_pending=True)

def init_db():
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS queue (
        id INTEGER PRIMARY KEY,
        file_id TEXT,
        caption TEXT,
        sent INTEGER DEFAULT 0,
        added_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

def get_setting(key, default=None):
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key, value):
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

# Кол-во фото за раз (по умолчанию 3)
def get_batch_size():
    return int(get_setting('batch_size', '3'))

# ====================== /start ======================
@bot.message_handler(commands=['start'])
def start(message):
    if message.chat.id != ADMIN_ID:
        return
    bot.reply_to(message, "Бот готов! Кидай фото.\n\n/settings — настройки\n/queue — очередь\n/delete [n] — удалить последние n или все")

# ====================== Приём фото ======================
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    if message.chat.id != ADMIN_ID:
        bot.reply_to(message, "❌ Доступ запрещён")
        return
    file_id = message.photo[-1].file_id
    caption = message.caption or ""
    conn = sqlite3.connect('queue.db')
    conn.execute("INSERT INTO queue (file_id, caption, added_at) VALUES (?, ?, ?)",
                 (file_id, caption, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    bot.reply_to(message, f"✅ Добавлено! Осталось: {get_pending_count()}")

# ====================== /queue ======================
@bot.message_handler(commands=['queue'])
def show_queue(message):
    if message.chat.id != ADMIN_ID: return
    conn = sqlite3.connect('queue.db')
    rows = conn.execute("SELECT id, caption, added_at FROM queue WHERE sent=0 ORDER BY id LIMIT 20").fetchall()
    conn.close()
    if not rows:
        bot.reply_to(message, "Очередь пуста")
        return
    text = "📋 Очередь:\n" + "\n".join([f"#{r[0]} | {r[2][:10]} | {(r[1] or 'без подписи')[:60]}" for r in rows])
    bot.reply_to(message, text)

# ====================== /delete [n] ======================
@bot.message_handler(commands=['delete'])
def delete_queue(message):
    if message.chat.id != ADMIN_ID: return

    args = message.text.split()
    if len(args) == 1:
        # Удалить все
        conn = sqlite3.connect('queue.db')
        conn.execute("DELETE FROM queue WHERE sent=0")
        conn.commit()
        conn.close()
        bot.reply_to(message, "Вся очередь очищена!")
        return

    try:
        n = int(args[1])
        if n < 1:
            bot.reply_to(message, "Укажи положительное число")
            return
    except:
        bot.reply_to(message, "Формат: /delete [число] или просто /delete для всей очереди")
        return

    conn = sqlite3.connect('queue.db')
    # Последние n = самые новые (ORDER BY id DESC LIMIT n)
    to_delete = conn.execute(
        "SELECT id FROM queue WHERE sent=0 ORDER BY id DESC LIMIT ?",
        (n,)
    ).fetchall()

    if not to_delete:
        bot.reply_to(message, "Нечего удалять")
        conn.close()
        return

    ids = [row[0] for row in to_delete]
    placeholders = ','.join('?' for _ in ids)
    conn.execute(f"DELETE FROM queue WHERE id IN ({placeholders})", ids)
    conn.commit()
    conn.close()

    bot.reply_to(message, f"Удалено {len(ids)} последних фото из очереди")

# ====================== /settings ======================
@bot.message_handler(commands=['settings'])
def settings_menu(message):
    if message.chat.id != ADMIN_ID: return

    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    current_batch = get_batch_size()
    markup.add(
        telebot.types.InlineKeyboardButton(f"Фото за раз: {current_batch}", callback_data="dummy"),
        telebot.types.InlineKeyboardButton("Изменить кол-во", callback_data="change_batch")
    )
    # Если хочешь добавить выбор канала позже — здесь будет кнопка
    # markup.add(telebot.types.InlineKeyboardButton("Выбрать канал", callback_data="change_channel"))

    bot.reply_to(message, "Настройки бота:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_settings(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Доступ запрещён")
        return

    if call.data == "change_batch":
        markup = telebot.types.InlineKeyboardMarkup(row_width=5)
        for i in range(1, 21):
            markup.add(telebot.types.InlineKeyboardButton(str(i), callback_data=f"set_batch_{i}"))
        bot.edit_message_text("Выбери количество фото за один запуск (1–20):", call.message.chat.id, call.message.message_id, reply_markup=markup)

    elif call.data.startswith("set_batch_"):
        num = int(call.data.split("_")[2])
        set_setting('batch_size', str(num))
        bot.edit_message_text(f"Установлено: {num} фото за раз", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, f"Сохранено: {num}")

# ====================== Отправка (теперь использует настройку) ======================
@app.route('/send-now')
def send_photos():
    if request.args.get('secret') != SECRET:
        return 'Access denied', 403

    batch_size = get_batch_size()

    conn = sqlite3.connect('queue.db')
    photos = conn.execute(
        f"SELECT id, file_id, caption FROM queue WHERE sent=0 ORDER BY id LIMIT {batch_size}"
    ).fetchall()

    sent_count = 0
    for pid, file_id, caption in photos:
        try:
            bot.send_photo(CHANNEL_ID, file_id, caption=caption or None)
            conn.execute("UPDATE queue SET sent=1 WHERE id=?", (pid,))
            sent_count += 1
        except Exception as e:
            print(f"Ошибка отправки #{pid}: {e}")

    conn.commit()
    conn.close()
    return f'Отправлено {sent_count} фото', 200

# ... остальной код (webhook, setwebhook, home, ping если был) остаётся без изменений ...

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
