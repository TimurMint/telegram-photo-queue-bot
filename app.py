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
    c.execute('''CREATE TABLE IF NOT EXISTS admin_channels (
        chat_id TEXT PRIMARY KEY,
        title TEXT,
        added_at TEXT
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

def get_current_channel():
    return get_setting('current_channel')

def set_current_channel(chat_id):
    set_setting('current_channel', chat_id)

def get_pending_count():
    conn = sqlite3.connect('queue.db')
    count = conn.execute("SELECT COUNT(*) FROM queue WHERE sent=0").fetchone()[0]
    conn.close()
    return count

# ====================== АВТОМАТИЧЕСКОЕ ОТСЛЕЖИВАНИЕ КАНАЛОВ ======================
@bot.my_chat_member_handler()
def track_channels(update):
    chat = update.chat_member.chat
    member = update.chat_member.new_chat_member
    if member.status in ["administrator", "creator"] and chat.type in ["channel", "supergroup"]:
        conn = sqlite3.connect('queue.db')
        conn.execute("""INSERT OR REPLACE INTO admin_channels (chat_id, title, added_at) 
                        VALUES (?, ?, ?)""",
                     (str(chat.id), chat.title or chat.username or "Без названия", datetime.now().isoformat()))
        conn.commit()
        conn.close()
        print(f"Бот добавлен как админ в канал: {chat.title} ({chat.id})")

# ====================== КОМАНДЫ ======================
@bot.message_handler(commands=['start'])
def start(message):
    if message.chat.id not in ADMIN_IDS:
        return
    bot.reply_to(message, "✅ Бот готов к работе!\n\n"
                          "Команды:\n"
                          "/settings — настройки бота\n"
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
        channel = get_current_channel()
        if not channel:
            bot.reply_to(message, "Сначала выберите канал в /settings")
            return
        bot.send_photo(channel, file_id, caption=caption or None)
        conn.execute("UPDATE queue SET sent=1 WHERE id=?", (pid,))
        conn.commit()
        bot.reply_to(message, f"✅ Отправлено фото #{pid} прямо сейчас!")
    except Exception as e:
        bot.reply_to(message, f"Ошибка отправки: {e}")
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
                bot.reply_to(message, f"✅ Удалено {len(ids)} последних фото из очереди")
            else:
                bot.reply_to(message, "Нечего удалять")
        except:
            bot.reply_to(message, "Используйте: /delete или /delete 5")
    conn.close()

# ====================== НАСТРОЙКИ ======================
@bot.message_handler(commands=['settings'])
def settings_menu(message):
    if message.chat.id not in ADMIN_IDS: return

    current_batch = get_batch_size()
    current_channel_id = get_current_channel()
    current_channel_name = "Не выбран"

    if current_channel_id:
        conn = sqlite3.connect('queue.db')
        row = conn.execute("SELECT title FROM admin_channels WHERE chat_id=?", (current_channel_id,)).fetchone()
        conn.close()
        if row:
            current_channel_name = row[0]

    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    markup.add(telebot.types.InlineKeyboardButton(f"📸 Фото за раз: {current_batch}", callback_data="dummy"))
    markup.add(telebot.types.InlineKeyboardButton("Изменить количество фото", callback_data="change_batch"))
    markup.add(telebot.types.InlineKeyboardButton(f"📢 Канал: {current_channel_name}", callback_data="change_channel"))

    bot.reply_to(message, "⚙️ Настройки бота:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Доступ запрещён")
        return

    if call.data == "change_batch":
        markup = telebot.types.InlineKeyboardMarkup(row_width=5)
        for i in range(1, 21):
            markup.add(telebot.types.InlineKeyboardButton(str(i), callback_data=f"set_batch_{i}"))
        bot.edit_message_text("Сколько фото отправлять за один запуск?", 
                              call.message.chat.id, call.message.message_id, reply_markup=markup)

    elif call.data.startswith("set_batch_"):
        num = int(call.data.split("_")[2])
        set_setting('batch_size', num)
        bot.edit_message_text(f"✅ Установлено: {num} фото за раз", call.message.chat.id, call.message.message_id)

    elif call.data == "change_channel":
        conn = sqlite3.connect('queue.db')
        channels = conn.execute("SELECT chat_id, title FROM admin_channels").fetchall()
        conn.close()

        if not channels:
            bot.answer_callback_query(call.id, "Бот ещё не добавлен ни в один канал как администратор")
            return

        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        for chat_id, title in channels:
            markup.add(telebot.types.InlineKeyboardButton(title or chat_id, callback_data=f"set_channel_{chat_id}"))
        bot.edit_message_text("Выберите канал для отправки фото:", 
                              call.message.chat.id, call.message.message_id, reply_markup=markup)

    elif call.data.startswith("set_channel_"):
        chat_id = call.data.split("_")[2]
        set_current_channel(chat_id)
        conn = sqlite3.connect('queue.db')
        title = conn.execute("SELECT title FROM admin_channels WHERE chat_id=?", (chat_id,)).fetchone()
        conn.close()
        title = title[0] if title else chat_id
        bot.edit_message_text(f"✅ Канал изменён на:\n{title}", call.message.chat.id, call.message.message_id)

# ====================== ОТПРАВКА ПО РАСПИСАНИЮ ======================
@app.route('/send-now')
def send_photos():
    if request.args.get('secret') != SECRET:
        return 'Access denied', 403

    channel = get_current_channel()
    if not channel:
        return 'Канал не выбран в настройках!', 200

    batch = get_batch_size()
    conn = sqlite3.connect('queue.db')
    photos = conn.execute(
        "SELECT id, file_id, caption FROM queue WHERE sent=0 ORDER BY id LIMIT ?", (batch,)
    ).fetchall()

    sent_count = 0
    for pid, file_id, caption in photos:
        try:
            bot.send_photo(channel, file_id, caption=caption or None)
            conn.execute("UPDATE queue SET sent=1 WHERE id=?", (pid,))
            sent_count += 1
        except Exception as e:
            print(f"Ошибка отправки #{pid}: {e}")
    conn.commit()
    conn.close()
    return f'Отправлено {sent_count} фото', 200

# ====================== СЛУЖЕБНЫЕ МАРШРУТЫ ======================
@app.route(f'/{SECRET}', methods=['POST'])
def webhook():
    try:
        update = telebot.types.Update.de_json(request.get_json(force=True))
        bot.process_new_updates([update])
        return 'OK', 200
    except Exception as e:
        print("Webhook error:", e)
        return 'OK', 200

@app.route('/ping')
def ping():
    return 'Pong', 200

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
