import telebot
from flask import Flask, request
import sqlite3
import os
from datetime import datetime
import threading

app = Flask(__name__)

# ================== НАСТРОЙКИ ==================
TOKEN      = os.getenv('TOKEN')
# Несколько админов через запятую, например: 613728374,987654321
ADMIN_IDS  = [int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()]
CHANNEL_ID = os.getenv('CHANNEL_ID')
SECRET     = os.getenv('SECRET')
# ==============================================

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

def get_setting(key, default="3"):
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key, value):
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def get_batch_size():
    return int(get_setting('batch_size', '3'))

def get_pending_count():
    conn = sqlite3.connect('queue.db')
    count = conn.execute("SELECT COUNT(*) FROM queue WHERE sent=0").fetchone()[0]
    conn.close()
    return count

# ====================== КОМАНДЫ ======================
@bot.message_handler(commands=['start'])
def start(message):
    if message.chat.id not in ADMIN_IDS:
        return
    bot.reply_to(message, "✅ Бот работает!\n\n"
                          "Команды:\n"
                          "/settings — изменить количество фото за раз\n"
                          "/queue — посмотреть очередь\n"
                          "/sendone — отправить одно фото сейчас\n"
                          "/delete [N] — удалить последние N фото\n"
                          "/delete — очистить всю очередь")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    if message.chat.id not in ADMIN_IDS:
        bot.reply_to(message, "❌ Доступ запрещён")
        return

    file_id = message.photo[-1].file_id
    caption = message.caption or ""

    conn = sqlite3.connect('queue.db')
    conn.execute("INSERT INTO queue (file_id, caption, added_at) VALUES (?, ?, ?)",
                 (file_id, caption, datetime.now().isoformat()))
    conn.commit()
    conn.close()

    bot.reply_to(message, f"✅ Фото добавлено в очередь!\nОсталось: {get_pending_count()}")

@bot.message_handler(commands=['queue'])
def show_queue(message):
    if message.chat.id not in ADMIN_IDS: return
    conn = sqlite3.connect('queue.db')
    rows = conn.execute("SELECT id, caption, added_at FROM queue WHERE sent=0 ORDER BY id LIMIT 20").fetchall()
    conn.close()
    if not rows:
        bot.reply_to(message, "Очередь пуста")
        return
    text = "📋 Очередь:\n" + "\n".join([f"#{r[0]} | {r[2][:10]} | {(r[1] or 'без подписи')[:60]}" for r in rows])
    bot.reply_to(message, text)

@bot.message_handler(commands=['sendone'])
def send_one_now(message):
    if message.chat.id not in ADMIN_IDS: return
    conn = sqlite3.connect('queue.db')
    photo = conn.execute("SELECT id, file_id, caption FROM queue WHERE sent=0 ORDER BY id LIMIT 1").fetchone()
    if not photo:
        bot.reply_to(message, "Очередь пустая")
        conn.close()
        return
    pid, file_id, caption = photo
    try:
        bot.send_photo(CHANNEL_ID, file_id, caption=caption or None)
        conn.execute("UPDATE queue SET sent=1 WHERE id=?", (pid,))
        conn.commit()
        bot.reply_to(message, f"✅ Отправлено фото #{pid} прямо сейчас!")
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")
    conn.close()

@bot.message_handler(commands=['delete'])
def delete_queue(message):
    if message.chat.id not in ADMIN_IDS: return
    args = message.text.split()
    conn = sqlite3.connect('queue.db')
    if len(args) == 1:
        conn.execute("DELETE FROM queue WHERE sent=0")
        conn.commit()
        bot.reply_to(message, "✅ Вся очередь очищена!")
    else:
        try:
            n = int(args[1])
            ids = [row[0] for row in conn.execute(
                "SELECT id FROM queue WHERE sent=0 ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()]
            if ids:
                placeholders = ','.join('?' * len(ids))
                conn.execute(f"DELETE FROM queue WHERE id IN ({placeholders})", ids)
                conn.commit()
                bot.reply_to(message, f"✅ Удалено {len(ids)} последних фото")
            else:
                bot.reply_to(message, "Нечего удалять")
        except:
            bot.reply_to(message, "Формат: /delete или /delete 5")
    conn.close()

# ====================== НАСТРОЙКИ ======================
@bot.message_handler(commands=['settings'])
def settings_menu(message):
    if message.chat.id not in ADMIN_IDS: return
    current = get_batch_size()
    markup = telebot.types.InlineKeyboardMarkup(row_width=5)
    for i in range(1, 21):
        markup.add(telebot.types.InlineKeyboardButton(str(i), callback_data=f"set_{i}"))
    bot.reply_to(message, f"Текущее количество: {current} фото за раз\n\nВыберите новое:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Доступ запрещён")
        return
    if call.data.startswith("set_"):
        num = int(call.data.split("_")[1])
        set_setting('batch_size', num)
        bot.edit_message_text(f"✅ Установлено: {num} фото за один запуск",
                              call.message.chat.id, call.message.message_id)

# ====================== ОТПРАВКА ПО РАСПИСАНИЮ ======================
@app.route('/send-now')
def send_photos():
    if request.args.get('secret') != SECRET:
        return 'Access denied', 403

    batch = get_batch_size()
    conn = sqlite3.connect('queue.db')
    photos = conn.execute(
        "SELECT id, file_id, caption FROM queue WHERE sent=0 ORDER BY id LIMIT ?", (batch,)
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

# ====================== ЗАЩИТА ОТ ПЕРЕЗАПУСКА (очень важно!) ======================
@app.route('/ping')
def ping():
    return 'Pong', 200

# ====================== ВЕБХУК ======================
@app.route(f'/{SECRET}', methods=['POST'])
def webhook():
    try:
        update = telebot.types.Update.de_json(request.get_json(force=True))
        bot.process_new_updates([update])
        return 'OK', 200
    except Exception as e:
        print("Webhook error:", e)
        return 'OK', 200

@app.route('/setwebhook')
def setup_webhook():
    if request.args.get('key') != SECRET:
        return 'Wrong key', 403
    url = f"https://{request.host}/{SECRET}"
    bot.remove_webhook()
    bot.set_webhook(url=url)
    return f'✅ Webhook установлен!\nURL: {url}'

@app.route('/')
def home():
    return "Бот работает ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
