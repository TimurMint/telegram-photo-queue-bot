import telebot
from flask import Flask, request
import sqlite3
import os
from datetime import datetime

app = Flask(__name__)

# ================== НАСТРОЙКИ (берутся из Render) ==================
TOKEN = os.getenv('8721130396:AAEhbvR-WpRrVMAMGiWxECjIx0iIQTqQVNE')
CHANNEL_ID = os.getenv('-1003619824382')      # @channel или -1001234567890
ADMIN_ID = int(os.getenv('613728374'))     # твой Telegram ID
SECRET = os.getenv('123secrethasr')              # любой длинный пароль, например supersecret12345
# =================================================================

bot = telebot.TeleBot(TOKEN)

def init_db():
    conn = sqlite3.connect('queue.db')
    conn.execute('''CREATE TABLE IF NOT EXISTS queue (
        id INTEGER PRIMARY KEY,
        file_id TEXT,
        caption TEXT,
        sent INTEGER DEFAULT 0,
        added_at TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

def get_pending_count():
    conn = sqlite3.connect('queue.db')
    count = conn.execute("SELECT COUNT(*) FROM queue WHERE sent=0").fetchone()[0]
    conn.close()
    return count

# ====================== ПРИЁМ ФОТО ======================
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

    bot.reply_to(message, f"✅ Фото добавлено в очередь!\nОсталось: {get_pending_count()}")

# ====================== КОМАНДЫ ======================
@bot.message_handler(commands=['queue'])
def show_queue(message):
    if message.chat.id != ADMIN_ID: return
    conn = sqlite3.connect('queue.db')
    rows = conn.execute("SELECT id, caption, added_at FROM queue WHERE sent=0 ORDER BY id LIMIT 20").fetchall()
    conn.close()
    if not rows:
        bot.reply_to(message, "Очередь пуста")
        return
    text = "📋 В очереди:\n" + "\n".join([f"#{r[0]} — {r[2][:10]} — { (r[1] or 'без подписи')[:50]}" for r in rows])
    bot.reply_to(message, text)

# ====================== ВЕБХУК ======================
@app.route(f'/{SECRET}', methods=['POST'])
def webhook():
    json_str = request.stream.read().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return 'OK', 200

# ====================== ТРИГГЕР ОТПРАВКИ (для cron) ======================
@app.route('/send-now')
def send_photos():
    if request.args.get('secret') != SECRET:
        return 'Access denied', 403

    conn = sqlite3.connect('queue.db')
    photos = conn.execute(
        "SELECT id, file_id, caption FROM queue WHERE sent=0 ORDER BY id LIMIT 3"
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
    return f'✅ Отправлено {sent_count} фото сегодня!', 200

# ====================== УСТАНОВКА ВЕБХУКА ======================
@app.route('/setwebhook')
def setup_webhook():
    if request.args.get('key') != SECRET:
        return 'Wrong key', 403
    webhook_url = f"https://{request.host}/{SECRET}"
    bot.remove_webhook()
    bot.set_webhook(url=webhook_url)
    return f'✅ Webhook установлен: {webhook_url}'

# Запуск (для Render не используется, но оставляем)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
