#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Tunefy Bot - скачивание музыки с YouTube
# Поддерживает Flask для работы на Render (keep-alive)
# Требования: pip install pyTelegramBotAPI yt-dlp flask

import sqlite3
import os
import re
import time
import threading
from pathlib import Path
from typing import List, Tuple, Optional

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
import yt_dlp
from flask import Flask, request, jsonify

# ========== КОНФИГУРАЦИЯ ==========
TOKEN = os.getenv("BOT_TOKEN", "8779429330:AAEzIsdj731gaq9G6e3cLLtg8xZvYN6FOOs")  # Для Render используй переменную окружения
DB_FILE = "tunefy.db"
TEMP_DIR = Path("downloads")
TEMP_DIR.mkdir(exist_ok=True)

# Настройки yt-dlp для поиска и скачивания MP3
YDL_OPTS_SEARCH = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "force_generic_extractor": False,
}

YDL_OPTS_DOWNLOAD = {
    "format": "bestaudio/best",
    "postprocessors": [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "192",
    }],
    "outtmpl": str(TEMP_DIR / "%(title).50s_%(id)s.%(ext)s"),
    "quiet": True,
    "no_warnings": True,
    "retries": 3,
    "ignoreerrors": True,
}

# ========== БАЗА ДАННЫХ ==========
def init_db():
    """Создаёт таблицу users, если её нет"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            requests_left INTEGER DEFAULT 100
        )
    """)
    conn.commit()
    conn.close()

def get_requests_left(user_id: int) -> int:
    """Возвращает количество оставшихся запросов у пользователя"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT requests_left FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0]
    else:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO users (user_id, requests_left) VALUES (?, 100)", (user_id,))
        conn.commit()
        conn.close()
        return 100

def decrement_requests(user_id: int) -> bool:
    """Уменьшает счётчик запросов на 1. Возвращает True если успешно, False если лимит 0"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT requests_left FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row or row[0] <= 0:
        conn.close()
        return False
    new_val = row[0] - 1
    c.execute("UPDATE users SET requests_left = ? WHERE user_id = ?", (new_val, user_id))
    conn.commit()
    conn.close()
    return True

def add_requests(user_id: int, amount: int):
    """Добавляет указанное количество запросов (после оплаты)"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET requests_left = requests_left + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

# ========== ПОИСК ПЕСЕН ==========
def search_songs(query: str, max_results: int = 5) -> List[Tuple[str, str, str]]:
    """
    Ищет песни на YouTube по запросу.
    Возвращает список кортежей: (название, video_url, thumbnail_url)
    """
    search_query = f"ytsearch{max_results}:{query}"
    with yt_dlp.YoutubeDL(YDL_OPTS_SEARCH) as ydl:
        info = ydl.extract_info(search_query, download=False)
        entries = info.get("entries", [])
        results = []
        for e in entries:
            title = e.get("title", "Без названия")
            url = e.get("url") or e.get("webpage_url")
            thumb = e.get("thumbnail", "")
            if url:
                results.append((title, url, thumb))
        return results

# ========== СКАЧИВАНИЕ И ОТПРАВКА ==========
def download_mp3(video_url: str) -> Optional[Path]:
    """Скачивает аудио с YouTube и возвращает путь к MP3-файлу"""
    try:
        with yt_dlp.YoutubeDL(YDL_OPTS_DOWNLOAD) as ydl:
            info = ydl.extract_info(video_url, download=True)
            filename = ydl.prepare_filename(info).replace(".webm", ".mp3").replace(".m4a", ".mp3")
            path = Path(filename)
            if path.exists():
                return path
            for f in TEMP_DIR.glob("*.mp3"):
                if f.stat().st_mtime > time.time() - 60:
                    return f
            return None
    except Exception as e:
        print(f"Download error: {e}")
        return None

def send_audio_with_tags(bot: telebot.TeleBot, chat_id: int, file_path: Path, title: str, performer: str):
    """Отправляет MP3 с метаданными"""
    with open(file_path, "rb") as f:
        bot.send_audio(
            chat_id,
            audio=f,
            title=title[:64],
            performer=performer[:64],
            caption="🎵 Твой трек от Tunefy",
            timeout=60
        )

def delete_file_after_delay(path: Path, delay_seconds: int = 300):
    """Удаляет файл через заданное время"""
    def delete():
        time.sleep(delay_seconds)
        if path.exists():
            path.unlink()
    threading.Thread(target=delete, daemon=True).start()

# ========== ОБРАБОТЧИКИ ТЕЛЕГРАМ БОТА ==========
bot = telebot.TeleBot(TOKEN, parse_mode=None)

@bot.message_handler(commands=['start'])
def start_command(message: Message):
    user_id = message.from_user.id
    get_requests_left(user_id)  # инициализация
    bot.send_message(
        message.chat.id,
        "🎵 Добро пожаловать в Tunefy!\n"
        "Просто отправь мне название песни или исполнителя, и я найду трек, скачаю и пришлю тебе MP3.\n"
        "Бот полностью бесплатен (пока не закончатся скрытые лимиты 😉)."
    )

@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_search(message: Message):
    user_id = message.from_user.id
    query = message.text.strip()
    if not query:
        return

    left = get_requests_left(user_id)
    if left <= 0:
        send_payment_invoice(message.chat.id, user_id)
        return

    bot.send_chat_action(message.chat.id, 'typing')
    bot.reply_to(message, f"🔍 Ищу: {query}...")
    songs = search_songs(query, max_results=5)
    if not songs:
        bot.reply_to(message, "Ничего не найдено. Попробуй другой запрос.")
        return

    keyboard = InlineKeyboardMarkup(row_width=1)
    for idx, (title, url, thumb) in enumerate(songs):
        btn_text = f"{idx+1}. {title[:50]}"
        callback_data = f"select|{url}|{title[:80]}"
        keyboard.add(InlineKeyboardButton(btn_text, callback_data=callback_data))
    bot.send_message(
        message.chat.id,
        "✅ Найдено. Выбери песню:",
        reply_markup=keyboard
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("select|"))
def handle_song_selection(call):
    user_id = call.from_user.id
    left = get_requests_left(user_id)
    if left <= 0:
        bot.answer_callback_query(call.id, "Лимит закончился. Используй кнопку оплаты.", show_alert=True)
        send_payment_invoice(call.message.chat.id, user_id)
        return

    data = call.data.split("|", 2)
    if len(data) < 3:
        bot.answer_callback_query(call.id, "Ошибка ссылки")
        return
    _, video_url, title = data
    bot.answer_callback_query(call.id, "Скачиваю, подожди...")
    bot.edit_message_text(
        f"⏳ Скачиваю: {title}...\nПожалуйста, подожди.",
        call.message.chat.id,
        call.message.message_id
    )

    def download_and_send():
        file_path = download_mp3(video_url)
        if file_path and file_path.exists():
            if decrement_requests(user_id):
                try:
                    send_audio_with_tags(bot, call.message.chat.id, file_path, title, title)
                except Exception as e:
                    bot.send_message(call.message.chat.id, f"Ошибка отправки: {e}")
                delete_file_after_delay(file_path, 300)
                bot.edit_message_text(
                    f"✅ Готово! {title} отправлен.",
                    call.message.chat.id,
                    call.message.message_id
                )
            else:
                bot.send_message(call.message.chat.id, "❌ Недостаточно запросов. Пополни баланс.")
                send_payment_invoice(call.message.chat.id, user_id)
        else:
            bot.send_message(call.message.chat.id, "❌ Не удалось скачать трек. Попробуй другой вариант.")

    threading.Thread(target=download_and_send, daemon=True).start()

# ========== ОПЛАТА ЧЕРЕЗ TELEGRAM STARS ==========
def send_payment_invoice(chat_id: int, user_id: int):
    """Отправляет инвойс на 10 Telegram Stars за 100 запросов"""
    bot.send_invoice(
        chat_id=chat_id,
        title="100 запросов в Tunefy",
        description="Пополни баланс и продолжай скачивать музыку без ограничений.",
        invoice_payload=f"add_100_{user_id}",
        provider_token="",
        currency="XTR",
        prices=[{"label": "100 запросов", "amount": 10}],
        start_parameter="tunefy_topup",
    )

@bot.pre_checkout_query_handler(func=lambda query: True)
def handle_pre_checkout(query):
    bot.answer_pre_checkout_query(query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def handle_successful_payment(message: Message):
    user_id = message.from_user.id
    # payload не используем, но можно распарсить для проверки
    add_requests(user_id, 100)
    bot.send_message(
        message.chat.id,
        "✅ Оплата прошла успешно! Тебе добавлено 100 запросов. Продолжай слушать музыку 🎶"
    )

# ========== FLASK ФЕРМА ДЛЯ KEEP-ALIVE ==========
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return jsonify({"status": "alive", "bot": "Tunefy"})

@flask_app.route('/health')
def health():
    return jsonify({"status": "ok"}), 200

def run_flask():
    """Запускает Flask сервер на порту, который задан Render (или 8080 по умолчанию)"""
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, debug=False)

# ========== ЗАПУСК БОТА И FLASK В РАЗНЫХ ПОТОКАХ ==========
if __name__ == "__main__":
    init_db()
    print("Бот Tunefy запущен. Flask ферма активна.")
    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    # Запускаем бота (блокирующий вызов)
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
