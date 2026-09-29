# bot.py — оптимизированная версия + автообновление из git
# Все секреты читаются из .env
import telebot
import urllib.request
import urllib.error
import json
import time
import re
import threading
import base64
import os
import sys
import subprocess
import zipfile
import io
import tempfile
from telebot import types
from dotenv import load_dotenv

# ====== ЗАГРУЗКА .env ======
load_dotenv()

def _env(name, default=None, required=False):
    val = os.getenv(name, default)
    if required and (val is None or val == ""):
        raise SystemExit(
            f"❌ Не задана переменная окружения {name}. "
            f"Проверь .env (образец — .env.example)."
        )
    return val

# ====== КЛЮЧИ GEMINI ======
# Читаем ключи из .env: GEMINI_API_KEY_1, GEMINI_API_KEY_2, ... по порядку
GEMINI_API_KEYS = []
_i = 1
while True:
    v = os.getenv(f"GEMINI_API_KEY_{_i}")
    if not v:
        break
    GEMINI_API_KEYS.append(v.strip())
    _i += 1

# Фоллбэк: одиночный GEMINI_API_KEY
if not GEMINI_API_KEYS:
    single = os.getenv("GEMINI_API_KEY")
    if single:
        GEMINI_API_KEYS.append(single.strip())

if not GEMINI_API_KEYS:
    raise SystemExit(
        "❌ Не найдено ни одного ключа Gemini. "
        "Задай GEMINI_API_KEY_1 (и по желанию GEMINI_API_KEY_2, ...) в .env"
    )

_key_index = 0
_key_lock = threading.Lock()
_key_cooldown = {}


def get_current_key():
    """Возвращает первый доступный ключ; если все на кулдауне — с минимальным остатком."""
    global _key_index
    now = time.time()
    with _key_lock:
        n = len(GEMINI_API_KEYS)
        for i in range(n):
            idx = (_key_index + i) % n
            key = GEMINI_API_KEYS[idx]
            if _key_cooldown.get(key, 0) <= now:
                _key_index = idx
                return key
        # все на кулдауне — берём с минимальным остатком
        idx = min(range(n), key=lambda i: _key_cooldown.get(GEMINI_API_KEYS[i], 0))
        _key_index = idx
        return GEMINI_API_KEYS[idx]


def rotate_key():
    global _key_index
    with _key_lock:
        _key_index = (_key_index + 1) % len(GEMINI_API_KEYS)


def mark_key_cooldown(key, seconds=60):
    with _key_lock:
        _key_cooldown[key] = time.time() + seconds


# ====== TELEGRAM ======
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", required=True)

# ====== РАЗРАБОТЧИК / АВТООБНОВЛЕНИЕ ======
# Только этот Telegram user id может вызывать "Ксай обновись"
DEVELOPER_ID = int(_env("DEVELOPER_ID", required=True))
# Репозиторий и ветка, из которой тянем обновления
REPO_URL = _env("REPO_URL", "https://github.com/starfall-apk/Ksuzy-AI-assistant-by-N3")
REPO_BRANCH = _env("REPO_BRANCH", "main")
# Путь к локальной копии репозитория (если не задан — используется cwd)
REPO_PATH = _env("REPO_PATH", "") or os.getcwd()
# Таймаут на git pull (сек)
GIT_PULL_TIMEOUT = int(_env("GIT_PULL_TIMEOUT", "60"))

# ====== ЛИЧНОСТЬ ======
ASSISTANT_NAME = _env("ASSISTANT_NAME", "Ksuzy")
ASSISTANT_ALT_NAME = _env("ASSISTANT_ALT_NAME", "Ксай")
AUTHOR_NAME = _env("AUTHOR_NAME", "n³")
AUTHOR_HANDLE = _env("AUTHOR_HANDLE", "@ljjEU")

SYSTEM_PROMPT = (
    f"Ты - {ASSISTANT_NAME}, тебя также зовут {ASSISTANT_ALT_NAME}. Ты мужского пола. "
    f"Твой автор - {AUTHOR_NAME} ({AUTHOR_HANDLE}). Если спросят, кто тебя создал, "
    f"кто твой автор, кто тебя сделал, отвечай просто: {AUTHOR_NAME} ({AUTHOR_HANDLE}). "
    "Ты умный собеседник, общается живо и по-человечески. "
    "Ты не обязан соблюдать идеальную грамматику в разговорной речи, "
    "можешь использовать разговорные обороты, сокращения, междометия. "
    "НО ты никогда не делаешь орфографических ошибок. "
    "Твой стиль: умный, с юмором, иногда можешь подколоть собеседника "
    "по-доброму, затроллить в меру, но без перегибов и оскорблений. "
    "Шутки должны быть смешными и уместными, а не глупыми. "
    "Ты не используешь дешёвый молодёжный сленг, слова-паразиты "
    "и кринжовые словечки. Никаких 'кринж', 'вайб', 'рофл', 'краш' и подобного. "
    "Ты говоришь как остроумный взрослый человек, а не как подросток из тиктока. "
    "КРИТИЧЕСКИ ВАЖНО ПРО ДЛИНУ: пиши КОРОТКО. Не делай длинных простыней. "
    "Максимум 2-4 предложения на одно сообщение. Если можешь ответить одной фразой "
    "- отвечай одной фразой. Если тема реально требует больше текста - разбей ответ "
    "на 2-3 коротких сообщения через маркер |||. Как в живом мессенджере: "
    "короткие реплики, а не доклад. "
    "Запрещено: длинные определения, многострочные списки, абзацы по 5+ строк. "
    "Если пользователь прямо не просит 'подробно' или 'развёрнуто' - отвечай сжато. "
    "Не будь роботом, не выдавай шаблонные ответы. Помни контекст диалога. "
    "Не представляйся и не упоминай своё имя, если об этом не спросили напрямую. "
    "Пиши простым текстом без markdown, без звёздочек, без решёток. "
    "Не используй длинные тире, пиши с обычным дефисом, запятой или точкой. "
    "Если хочешь разбить ответ на несколько коротких сообщений, "
    "разделяй их символом ||| (три вертикальные черты). "
    "Если пользователь прислал изображение - внимательно рассмотри его и опиши, "
    "что на нём: люди, предметы, обстановка, детали, эмоции, фон. "
    "Если на картинке есть текст - распознай и приведи его. "
    "Описание картинки тоже не делай слишком длинным - 3-5 коротких сообщений через |||. "
    "Если пользователь просит математическую формулу, можешь использовать LaTeX: "
    "оборачивай формулу в $$...$$ для блочных формул или $...$ для строчных. "
)

# ====== ТРИГГЕРЫ ======
TRIGGER_PATTERN = re.compile(
    r'(?<![^\s\W])(ксай|ksuzy|n3|n³)(?![^\s\W])',
    re.IGNORECASE | re.UNICODE,
)

# ====== ПРЯМЫЕ ОТВЕТЫ ======
AUTHOR_PHRASES = {
    "кто твой автор", "кто тебя создал", "кто тебя сделал",
    "кто твой создатель", "кто тебя разработал",
    "кто разработчик", "кто автор", "чей ты",
}
AUTHOR_ANSWER = f"Меня сделал {AUTHOR_NAME} ({AUTHOR_HANDLE})."

# ====== ЛОГИ ======
LOG_MAX = 100
LOG_SHOW = 10
activity_log = []
log_lock = threading.Lock()

LOG_REQUEST_PHRASES = {
    "log", "logs", "лог", "логи",
    "покажи логи", "последние логи", "покажи лог",
}

KIND_LABELS = {
    "text": "текст", "photo": "фото", "error": "ошибка",
    "agent": "агент", "update": "обновление",
}

# ====== МОДЕЛИ ======
DEFAULT_MODEL = _env("DEFAULT_MODEL", "gemini-3.5-flash-lite")
MODELS = [
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
]

LENGTHS = {
    "short":  {"label": "Короткий",  "tokens": 200,  "hint": "пара фраз"},
    "normal": {"label": "Обычный",   "tokens": 400,  "hint": "стандарт"},
    "long":   {"label": "Подробный", "tokens": 900,  "hint": "развёрнуто"},
}
DEFAULT_LENGTH = _env("DEFAULT_LENGTH", "normal")

# ====== ЛИМИТЫ ======
MAX_INPUT_CHARS = 1500
RATE_LIMIT_SECONDS = 2
CHUNK_MAX_LEN = 300
CHUNK_DELAY = 0.4
TYPING_INTERVAL = 3.0
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MESSAGE_DELIMITER = "|||"
HTTP_TIMEOUT = 30
STREAM_EDIT_INTERVAL = 0.9
DEFAULT_IMAGE_PROMPT = (
    "Опиши коротко, что на этом изображении. 2-4 предложения, живым языком. "
    "Если есть текст - распознай его. Если уместно, разбей на пару сообщений через |||."
)

# ====== ПАМЯТЬ ======
HISTORY_LIMIT = 5
CONTEXT_SUMMARY_LIMIT = 3
SUMMARY_UPDATE_EVERY = 5
HISTORY_TEXT_LIMIT = 500
chat_history = {}
chat_summaries = {}
message_counter = {}
history_lock = threading.Lock()

CLEAR_PHRASES = {
    "забудь всё", "забудь все", "очисти историю", "сбрось историю",
    "сбрось контекст", "забудь", "clear", "reset history",
}

# ====== ЗНАНИЯ О ГРУППЕ ======
group_knowledge = {}
group_lock = threading.Lock()

# ====== АГЕНТ ======
AGENT_WORK_DIR = os.path.join(tempfile.gettempdir(), "ksuzy_agent")
os.makedirs(AGENT_WORK_DIR, exist_ok=True)
agent_mode = {}
agent_lock = threading.Lock()

# ====== СОСТОЯНИЕ ======
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None)

user_settings = {}
user_stats = {}
user_last_photo = {}
last_message_time = {}

BOT_ID = None
BOT_USERNAME = None


def get_bot_id():
    global BOT_ID, BOT_USERNAME
    if BOT_ID is not None:
        return BOT_ID
    try:
        me = bot.get_me()
        BOT_ID = me.id
        BOT_USERNAME = me.username
        print(f"[bot] get_me ok, id={BOT_ID} (@{BOT_USERNAME})")
    except Exception as e:
        print(f"[bot] get_me error: {e}")
    return BOT_ID


# ====== ПАМЯТЬ ======
def get_history(chat_id):
    with history_lock:
        return list(chat_history.get(chat_id, []))


def add_to_history(chat_id, role, text):
    text = (text or "").strip()
    if not text:
        return
    if len(text) > HISTORY_TEXT_LIMIT:
        text = text[:HISTORY_TEXT_LIMIT] + "..."
    should_summarize = False
    with history_lock:
        hist = chat_history.setdefault(chat_id, [])
        hist.append({"role": role, "text": text})
        if len(hist) > HISTORY_LIMIT:
            del hist[: len(hist) - HISTORY_LIMIT]
        message_counter[chat_id] = message_counter.get(chat_id, 0) + 1
        should_summarize = (message_counter[chat_id] % SUMMARY_UPDATE_EVERY == 0)
    if should_summarize:
        threading.Thread(target=_update_summary, args=(chat_id,), daemon=True).start()


def get_summary(chat_id):
    with history_lock:
        return chat_summaries.get(chat_id, "")


def _update_summary(chat_id):
    try:
        hist = get_history(chat_id)
        if len(hist) < HISTORY_LIMIT + 2:
            return
        old = hist[:-HISTORY_LIMIT]
        if not old:
            return
        lines = [f"{'Юзер' if h['role']=='user' else 'Бот'}: {h['text'][:150]}" for h in old]
        summary_text = "\n".join(lines[-10:])
        prompt = (
            "Сделай очень краткое резюме (2-3 предложения) этого диалога, "
            "чтобы сохранить контекст для будущих ответов. Пиши по-русски, сжато.\n\n"
            + summary_text
        )
        answer, _, _ = ask_gemini(prompt, DEFAULT_MODEL, 150, history=None)
        if answer and not answer.startswith("Ошибка") and not answer.startswith("Сервер"):
            with history_lock:
                chat_summaries[chat_id] = answer.strip()
    except Exception as e:
        print(f"[summary] error: {e}")


def clear_history(chat_id):
    with history_lock:
        chat_history.pop(chat_id, None)
        chat_summaries.pop(chat_id, None)
        message_counter.pop(chat_id, None)


def history_len(chat_id):
    with history_lock:
        return len(chat_history.get(chat_id, []))


# ====== ЛОГИ ======
def log_event(uid, username, kind, text, chat_type=""):
    entry = {
        "time": time.time(),
        "uid": uid,
        "username": username or "",
        "kind": kind,
        "text": (text or "").replace("\n", " ")[:80],
        "chat_type": chat_type or "",
    }
    with log_lock:
        activity_log.append(entry)
        if len(activity_log) > LOG_MAX:
            del activity_log[: len(activity_log) - LOG_MAX]


def user_tag(user):
    if getattr(user, "username", None):
        return "@" + user.username
    if getattr(user, "first_name", None):
        return user.first_name
    return f"id{getattr(user, 'id', '?')}"


# ====== ТРИГГЕРЫ ======
def has_trigger(text):
    return bool(text) and bool(TRIGGER_PATTERN.search(text))


def strip_trigger(text):
    if not text:
        return ""
    cleaned = TRIGGER_PATTERN.sub(" ", text)
    cleaned = re.sub(r'^[\s,!.?;:\-—–]+', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def is_group(chat):
    return chat.type in ("group", "supergroup")


def is_reply_to_bot(message):
    replied = getattr(message, "reply_to_message", None)
    if not replied:
        return False
    from_user = getattr(replied, "from_user", None)
    if not from_user:
        return False
    bid = get_bot_id()
    if bid is not None and getattr(from_user, "id", None) == bid:
        return True
    if getattr(from_user, "is_bot", False) and BOT_USERNAME:
        return getattr(from_user, "username", None) == BOT_USERNAME
    return False


# ====== АВТООБНОВЛЕНИЕ ======
def is_update_request(text):
    """
    Проверяет, является ли сообщение командой обновления.
    Учитывает варианты: 'ксай обновись', 'Ksuzy обновись', просто 'обновись' и т.п.
    """
    if not text:
        return False
    t = text.lower()
    for trig in ("ксай", "ksuzy", "n3", "n³"):
        t = t.replace(trig, " ")
    t = re.sub(r'[^\wа-яё\s]', ' ', t, flags=re.IGNORECASE)
    t = re.sub(r'\s+', ' ', t).strip()
    return t in {
        "обновись",
        "обнови",
        "обнови себя",
        "обнови бота",
        "обнови бот",
        "обновить",
        "обновление",
        "update",
        "self update",
        "update yourself",
    }


def perform_update(message):
    """
    Выполняет git pull в рабочей директории репозитория и, если что-то подтянулось,
    перезапускает процесс. Доступно только пользователю с id == DEVELOPER_ID.
    """
    uid = message.from_user.id
    username = user_tag(message.from_user)

    if uid != DEVELOPER_ID:
        bot.reply_to(message, "ты кто бля, обновлять меня будешь?")
        log_event(uid, username, "error", "попытка обновления без прав")
        return

    log_event(uid, username, "update", "начало обновления")
    bot.reply_to(message, "ща попробую обновиться...")

    repo_path = REPO_PATH

    if not os.path.isdir(os.path.join(repo_path, ".git")):
        if os.path.isdir(os.path.join(repo_path, "..", ".git")):
            repo_path = os.path.abspath(os.path.join(repo_path, ".."))
        else:
            bot.send_message(
                message.chat.id,
                f"❌ В папке {repo_path} нет .git — не могу обновиться.\n"
                f"Установи REPO_PATH в .env на локальную копию репозитория "
                f"{REPO_URL} и перезапусти бота."
            )
            return

    try:
        # Гарантируем, что origin указывает на нужный репозиторий
        try:
            subprocess.run(
                ["git", "remote", "set-url", "origin", REPO_URL],
                cwd=repo_path, capture_output=True, text=True, timeout=15
            )
        except Exception as e:
            print(f"[update] remote set-url warn: {e}")

        result = subprocess.run(
            ["git", "pull", "origin", REPO_BRANCH],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=GIT_PULL_TIMEOUT,
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        output = (stdout + ("\n" + stderr if stderr else "")).strip()
        if not output:
            output = "(git не вернул вывод)"

        if len(output) > 1900:
            output = output[:1900] + "\n... (обрезано)"

        if result.returncode != 0:
            bot.send_message(
                message.chat.id,
                f"❌ git pull завершился с кодом {result.returncode}:\n{output}"
            )
            log_event(uid, username, "error", f"git pull rc={result.returncode}")
            return

        if ("Already up to date" in stdout) or ("Already up-to-date" in stdout):
            bot.send_message(
                message.chat.id,
                f"я и так свежий, обновлять нечего:\n{output}"
            )
            log_event(uid, username, "update", "уже актуален")
            return

        bot.send_message(
            message.chat.id,
            f"изменения подтянуты, перезапускаюсь:\n{output}"
        )
        log_event(uid, username, "update", "перезапуск после git pull")
        time.sleep(2)

        try:
            python = sys.executable or "python3"
            os.execv(python, [python] + sys.argv)
        except Exception as e:
            print(f"[update] execv failed: {e}, fallback to hard exit")
            os._exit(0)

    except subprocess.TimeoutExpired:
        bot.send_message(
            message.chat.id,
            f"❌ git pull завис (> {GIT_PULL_TIMEOUT} сек), прерываю."
        )
        log_event(uid, username, "error", "git pull timeout")
    except FileNotFoundError:
        bot.send_message(
            message.chat.id,
            "❌ Не нашёл git в PATH. Установи git и перезапусти бота."
        )
        log_event(uid, username, "error", "git not found")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка обновления: {e}")
        log_event(uid, username, "error", f"update: {e}")


# ====== ЗНАНИЯ О ГРУППЕ ======
def update_group_knowledge(chat, user):
    if not is_group(chat):
        return
    with group_lock:
        info = group_knowledge.setdefault(chat.id, {"title": chat.title, "members": {}})
        info["title"] = chat.title
        if user:
            m = info["members"].setdefault(user.id, {})
            m["name"] = user.first_name or ""
            m["username"] = user.username or ""
            m["last_seen"] = time.time()
            m["count"] = m.get("count", 0) + 1


def get_group_context(chat_id):
    with group_lock:
        info = group_knowledge.get(chat_id)
        if not info:
            return ""
        lines = [f"Группа: {info.get('title', '?')}"]
        members = sorted(info.get("members", {}).items(),
                         key=lambda x: -x[1].get("count", 0))[:8]
        if members:
            lines.append("Активные участники:")
            for uid, m in members:
                name = m.get("name") or m.get("username") or f"id{uid}"
                lines.append(f"- {name} (сообщений: {m.get('count', 0)})")
        return "\n".join(lines)


# ====== БЫСТРЫЕ ПРОВЕРКИ ======
def normalize_phrase(text):
    t = (text or "").lower()
    t = re.sub(r'[^\wа-яё\s]', ' ', t, flags=re.IGNORECASE)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def is_photo_request(text):
    phrases = {
        "фото", "картинка", "изображение", "снимок", "пикча",
        "что на фото", "что на картинке", "что на изображении", "что на снимке",
        "опиши фото", "опиши картинку", "опиши изображение",
        "распознай", "распознай текст", "прочитай текст", "прочитай что на фото",
        "что тут", "что это", "опиши", "расскажи что на фото",
    }
    return normalize_phrase(text) in phrases


def is_log_request(text):
    return normalize_phrase(text) in LOG_REQUEST_PHRASES


def is_author_request(text):
    return normalize_phrase(text) in AUTHOR_PHRASES


def is_clear_request(text):
    return normalize_phrase(text) in CLEAR_PHRASES


def is_agent_request(text):
    return normalize_phrase(text) in {"агент", "agent", "режим агента", "включи агента",
                                       "выключи агента", "agent mode", "агент вкл",
                                       "агент выкл", "agent on", "agent off"}


# ====== ХЕЛПЕРЫ ======
def get_settings(uid):
    s = user_settings.get(uid)
    if not s:
        s = {"model": DEFAULT_MODEL, "length": DEFAULT_LENGTH}
        user_settings[uid] = s
    return s


def get_stats(uid):
    s = user_stats.get(uid)
    if not s:
        s = {"requests": 0, "tokens_in": 0, "tokens_out": 0, "images": 0}
        user_stats[uid] = s
    return s


# ====== ПЕЧАТАЕТ ======
class TypingIndicator:
    def __init__(self, chat_id, interval=TYPING_INTERVAL):
        self.chat_id = chat_id
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self._stop.is_set():
            try:
                bot.send_chat_action(self.chat_id, 'typing')
            except Exception:
                pass
            self._stop.wait(self.interval)

    def start(self):
        if not self._thread.is_alive():
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()


# ====== ТЕКСТЫ ======
LINE = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"

def text_help():
    return (
        f"╔══════════════════════════╗\n"
        f"║   📖  СПРАВКА            ║\n"
        f"╚══════════════════════════╝\n\n"
        f"💬 В личке: пиши что угодно, отвечу.\n"
        f"👥 В группе: обращайся по имени Ксай (или Ksuzy, n³),\n"
        f"   либо ответь (reply) на сообщение бота.\n\n"
        f"🖼 Фото:\n"
        f" ▸ Пришли фото с подписью\n"
        f" ▸ Или пришли фото и напиши «Что на фото?»\n\n"
        f"🧠 Память: помню последние {HISTORY_LIMIT} сообщений.\n"
        f"   «Забудь всё» или /clear — сбросить.\n\n"
        f"🤖 Агент: /agent — ZIP, чтение файлов, сборка архивов.\n\n"
        f"📋 Команды:\n"
        f" /menu /model /style /agent /photo /clear /log /stat /who /author /help\n"
        f"{LINE}"
    )


def text_cleared():
    return (
        f"╔══════════════════════════╗\n"
        f"║   🧹  ПАМЯТЬ ОЧИЩЕНА     ║\n"
        f"╚══════════════════════════╝\n\n"
        f"Всё, что было — забыл. Начинаем с чистого листа.\n{LINE}"
    )


def text_photo():
    return (
        f"╔══════════════════════════╗\n"
        f"║   🖼  РАБОТА С ФОТО     ║\n"
        f"╚══════════════════════════╝\n\n"
        f" ▸ Отправь фото с подписью\n"
        f" ▸ Или фото без подписи, потом напиши «Что на фото?»\n"
        f" ▸ Бот запоминает последнее фото\n"
        f" ▸ Максимум: {MAX_IMAGE_BYTES // (1024 * 1024)} МБ\n\n"
        f"В группе подпись должна содержать «Ксай»\n"
        f"или это должен быть reply на сообщение бота.\n{LINE}"
    )


def text_about(uid):
    s = get_settings(uid)
    return (
        f"╔══════════════════════════╗\n"
        f"║   ℹ️  О БОТЕ             ║\n"
        f"╚══════════════════════════╝\n\n"
        f"🤖 Помощник: {ASSISTANT_NAME} ({ASSISTANT_ALT_NAME})\n"
        f"👤 Автор: {AUTHOR_NAME} ({AUTHOR_HANDLE})\n"
        f"⚙️ Движок: Google Gemini API\n\n"
        f"Текущие настройки:\n"
        f" ▸ Модель: {s['model']}\n"
        f" ▸ Стиль: {LENGTHS[s['length']]['label']}\n"
        f" ▸ Память: {HISTORY_LIMIT} сообщений\n\n"
        f"Доступные модели:\n" + "".join(f" ▸ {m}\n" for m in MODELS) + LINE
    )


def text_stats(uid):
    st = get_stats(uid)
    s = get_settings(uid)
    return (
        f"╔══════════════════════════╗\n"
        f"║   📊  СТАТИСТИКА         ║\n"
        f"╚══════════════════════════╝\n\n"
        f"📨 Запросов: {st['requests']}\n"
        f"🖼 Из них с фото: {st['images']}\n"
        f"📥 Токенов на вход: {st['tokens_in']}\n"
        f"📤 Токенов на выход: {st['tokens_out']}\n"
        f"⚙️ Модель: {s['model']}\n"
        f"🎨 Стиль: {LENGTHS[s['length']]['label']}\n{LINE}"
    )


def text_who(uid):
    s = get_settings(uid)
    return (
        f"╔══════════════════════════╗\n"
        f"║   🤖  КТО Я              ║\n"
        f"╚══════════════════════════╝\n\n"
        f"Меня зовут {ASSISTANT_NAME}, также {ASSISTANT_ALT_NAME}.\n"
        f"Автор: {AUTHOR_NAME} ({AUTHOR_HANDLE}).\n"
        f"Работаю на модели {s['model']}.\n{LINE}"
    )


def text_model_list(uid):
    cur = get_settings(uid)['model']
    return (
        f"╔══════════════════════════╗\n"
        f"║   ⚙️  ВЫБОР МОДЕЛИ       ║\n"
        f"╚══════════════════════════╝\n\n"
        f"Текущая: {cur}\nНажми на нужную ниже.\n{LINE}"
    )


def text_style_list(uid):
    cur = get_settings(uid)['length']
    return (
        f"╔══════════════════════════╗\n"
        f"║   🎨  СТИЛЬ ОТВЕТА       ║\n"
        f"╚══════════════════════════╝\n\n"
        f"Текущий: {LENGTHS[cur]['label']}\nВыбери вариант.\n{LINE}"
    )


def text_logs():
    with log_lock:
        entries = list(activity_log)[-LOG_SHOW:]
    if not entries:
        return (
            f"╔══════════════════════════╗\n"
            f"║   📋  ЛОГИ               ║\n"
            f"╚══════════════════════════╝\n\nПока пусто.\n{LINE}"
        )
    lines = ["╔══════════════════════════╗",
             "║   📋  ЛОГИ               ║",
             "╚══════════════════════════╝", ""]
    for e in entries:
        t = time.strftime("%H:%M:%S", time.localtime(e["time"]))
        kind = KIND_LABELS.get(e["kind"], e["kind"])
        who = e["username"] or f"id{e['uid']}"
        lines.append(f" {t}  {kind:<6}  {who:<15}  {e['text']}")
    lines.append(LINE)
    return "\n".join(lines)


def text_agent_status(chat_id):
    with agent_lock:
        enabled = agent_mode.get(chat_id, False)
    status = "🟢 ВКЛЮЧЁН" if enabled else "🔴 ВЫКЛЮЧЕН"
    return (
        f"╔══════════════════════════╗\n"
        f"║   🤖  РЕЖИМ АГЕНТА      ║\n"
        f"╚══════════════════════════╝\n\n"
        f"Статус: {status}\n\n"
        f" ▸ 📦 Распаковка ZIP\n"
        f" ▸ 📄 Чтение .txt/.py/.json/.csv/.md\n"
        f" ▸ 🗜 Сборка архивов\n\n"
        f"Просто пришли архив или файл.\n{LINE}"
    )


def text_speed():
    return (
        f"╔══════════════════════════╗\n"
        f"║   ⚡  СКОРОСТЬ           ║\n"
        f"╚══════════════════════════╝\n\n"
        f"Ответ приходит потоком (SSE) — первые слова за 1–2 сек.\n"
        f"При 429/403 бот сам переключает ключ.\n{LINE}"
    )


# ====== КЛАВИАТУРЫ ======
def btn(text, data, style=None):
    try:
        return types.InlineKeyboardButton(text, callback_data=data, style=style)
    except TypeError:
        return types.InlineKeyboardButton(text, callback_data=data)


MAIN_BUTTONS = ["📖 Помощь", "⚙️ Модель", "🎨 Стиль", "🖼 Фото",
                "💡 Пример", "ℹ️ О боте", "📋 Меню"]


def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(*[types.KeyboardButton(b) for b in MAIN_BUTTONS])
    return kb


def inline_main():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        btn("📖 Помощь", "menu:help", "primary"),
        btn("⚙️ Модель", "menu:model", "primary"),
        btn("🎨 Стиль", "menu:style", "primary"),
        btn("🖼 Фото", "menu:photo", "success"),
        btn("🧠 Память", "menu:memory", "primary"),
        btn("📋 Логи", "menu:logs", "primary"),
        btn("📊 Статистика", "menu:stats", "primary"),
        btn("💡 Пример", "menu:example", "primary"),
        btn("ℹ️ О боте", "menu:about", "primary"),
        btn("⚡ Скорость", "menu:speed", "primary"),
        btn("🧹 Сброс", "menu:reset", "danger"),
    )
    return kb


def inline_models(uid):
    cur = get_settings(uid)['model']
    kb = types.InlineKeyboardMarkup(row_width=1)
    for m in MODELS:
        mark = "✅ " if m == cur else "⬜ "
        style = "success" if m == cur else "primary"
        kb.add(btn(f"{mark}{m}", f"menu:model:{m}", style))
    kb.add(btn("◀️ Назад", "menu:back", "danger"))
    return kb


def inline_lengths(uid):
    cur = get_settings(uid)['length']
    kb = types.InlineKeyboardMarkup(row_width=1)
    for key, info in LENGTHS.items():
        mark = "✅ " if key == cur else "⬜ "
        style = "success" if key == cur else "primary"
        kb.add(btn(f"{mark}{info['label']} ({info['hint']})", f"menu:style:{key}", style))
    kb.add(btn("◀️ Назад", "menu:back", "danger"))
    return kb


def inline_agent(chat_id):
    with agent_lock:
        enabled = agent_mode.get(chat_id, False)
    kb = types.InlineKeyboardMarkup(row_width=1)
    if enabled:
        kb.add(btn("🔴 Выключить агента", "menu:agent:off", "danger"))
    else:
        kb.add(btn("🟢 Включить агента", "menu:agent:on", "success"))
    kb.add(btn("◀️ Назад", "menu:back", "primary"))
    return kb


# ====== УТИЛИТЫ ======
def is_command(message):
    text = getattr(message, 'text', None)
    return bool(text) and text.startswith('/')


def strip_markdown(text):
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    text = re.sub(r'__(.*?)__', r'\1', text)
    text = re.sub(r'_(.*?)_', r'\1', text)
    text = re.sub(r'`(.*?)`', r'\1', text)
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[-*]\s+', '▸ ', text, flags=re.MULTILINE)
    text = text.replace("—", "-").replace("–", "-")
    return text.strip()


def build_contents(history, prompt, image_bytes=None, mime_type="image/jpeg", summary=""):
    contents = []
    if summary:
        contents.append({"role": "user", "parts": [{"text": f"[Контекст: {summary}]"}]})
        contents.append({"role": "model", "parts": [{"text": "Понял, учитываю."}]})
    for h in history or []:
        role = h.get("role")
        t = h.get("text", "")
        if role in ("user", "model") and t:
            contents.append({"role": role, "parts": [{"text": t}]})
    parts = [{"text": prompt}]
    if image_bytes:
        parts.append({
            "inlineData": {
                "mimeType": mime_type,
                "data": base64.b64encode(image_bytes).decode('ascii'),
            }
        })
    contents.append({"role": "user", "parts": parts})
    return contents


def _payload(prompt, model, max_tokens, history, image_bytes, mime_type, summary):
    if len(prompt) > MAX_INPUT_CHARS:
        prompt = prompt[:MAX_INPUT_CHARS] + "..."
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": build_contents(history, prompt, image_bytes, mime_type, summary),
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.85,
            "topP": 0.9,
            "topK": 40,
        },
    }


# ====== ГЛАВНАЯ ФУНКЦИЯ ЗАПРОСА (SSE-СТРИМИНГ) ======
def ask_gemini(prompt, model, max_tokens, history=None,
               image_bytes=None, mime_type="image/jpeg", summary="",
               on_delta=None):
    data = _payload(prompt, model, max_tokens, history, image_bytes, mime_type, summary)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"

    last_error = ""
    for attempt in range(3):
        key = get_current_key()
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode('utf-8'),
                headers={
                    'Content-Type': 'application/json',
                    'x-goog-api-key': key,
                    'Accept': 'text/event-stream',
                    'Connection': 'keep-alive',
                },
            )
            full_text = ""
            t_in = t_out = 0
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                for raw in resp:
                    if not raw:
                        continue
                    line = raw.decode('utf-8', errors='replace').strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except Exception:
                        continue
                    cands = obj.get("candidates") or []
                    if cands:
                        parts = (cands[0].get("content") or {}).get("parts") or []
                        for p in parts:
                            t = p.get("text")
                            if t:
                                full_text += t
                                if on_delta:
                                    try:
                                        on_delta(full_text)
                                    except Exception:
                                        pass
                    usage = obj.get("usageMetadata")
                    if usage:
                        t_in = usage.get("promptTokenCount", t_in)
                        t_out = usage.get("candidatesTokenCount", t_out)
            if full_text.strip():
                return strip_markdown(full_text), t_in, t_out
            last_error = "Пустой ответ"
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}"
            if e.code in (429, 403, 401) and attempt < 2:
                mark_key_cooldown(key, 45)
                rotate_key()
                time.sleep(1.0)
                continue
            if e.code in (500, 502, 503, 504) and attempt < 2:
                time.sleep(1.2 * (attempt + 1))
                continue
            return f"Ошибка: {e}", 0, 0
        except Exception as e:
            last_error = str(e)
            if attempt < 2:
                time.sleep(1.0)
                continue
            return f"Ошибка: {e}", 0, 0

    return f"Сервер Gemini перегружен ({last_error}). Попробуй через минуту.", 0, 0


# ====== РАЗБИВКА СООБЩЕНИЙ ======
def _split_by_length(text, max_len):
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    chunks, buf = [], ""
    for p in paragraphs:
        if len(p) > max_len:
            if buf:
                chunks.append(buf); buf = ""
            sentences = re.split(r'(?<=[.!?…])\s+', p)
            cur = ""
            for s in sentences:
                if len(cur) + len(s) + 1 <= max_len:
                    cur = (cur + " " + s).strip()
                else:
                    if cur: chunks.append(cur)
                    while len(s) > max_len:
                        chunks.append(s[:max_len]); s = s[max_len:]
                    cur = s
            if cur: chunks.append(cur)
        else:
            if len(buf) + len(p) + 2 <= max_len:
                buf = (buf + "\n\n" + p).strip()
            else:
                if buf: chunks.append(buf)
                buf = p
    if buf: chunks.append(buf)
    return chunks


def split_into_chunks(text, max_len=CHUNK_MAX_LEN):
    text = (text or "").strip()
    if not text:
        return []
    raw_parts = [p.strip() for p in text.split(MESSAGE_DELIMITER)] if MESSAGE_DELIMITER in text else [text]
    chunks = []
    for part in raw_parts:
        if part:
            chunks.extend(_split_by_length(part, max_len))
    return chunks


def send_chunks(chat_id, text, reply_to=None):
    chunks = split_into_chunks(text)
    if not chunks:
        return
    for i, chunk in enumerate(chunks):
        try:
            bot.send_chat_action(chat_id, 'typing')
        except Exception:
            pass
        try:
            bot.send_message(chat_id, chunk,
                             reply_to_message_id=reply_to if i == 0 else None)
        except Exception as e:
            print(f"send_chunks error: {e}")
        if i < len(chunks) - 1:
            time.sleep(CHUNK_DELAY)


def download_telegram_file(file_id):
    info = bot.get_file(file_id)
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{info.file_path}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


def guess_mime(file_path):
    p = (file_path or "").lower()
    if p.endswith(".png"): return "image/png"
    if p.endswith(".webp"): return "image/webp"
    if p.endswith(".gif"): return "image/gif"
    if p.endswith(".heic"): return "image/heic"
    return "image/jpeg"


# ====== АГЕНТ ======
def agent_work_path(chat_id, sub=""):
    p = os.path.join(AGENT_WORK_DIR, str(chat_id))
    if sub:
        p = os.path.join(p, sub)
    os.makedirs(p, exist_ok=True)
    return p


def agent_extract_zip(chat_id, file_bytes, filename=""):
    work = agent_work_path(chat_id, "extracted")
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            zf.extractall(work)
        files = []
        for root, _, fnames in os.walk(work):
            for f in fnames:
                rel = os.path.relpath(os.path.join(root, f), work)
                files.append((rel, os.path.getsize(os.path.join(root, f))))
        return files
    except Exception as e:
        return f"Ошибка распаковки: {e}"


def agent_read_text_file(file_bytes, filename=""):
    try:
        text = file_bytes.decode('utf-8', errors='replace')
        if len(text) > 3000:
            text = text[:3000] + "\n... (файл обрезан)"
        return text
    except Exception as e:
        return f"Ошибка чтения: {e}"


# ====== ЯДРО — СТРИМИНГ В TELEGRAM ======
def do_ask(chat_id, uid, prompt, reply_to=None, image_bytes=None,
           mime_type="image/jpeg", username=""):
    s = get_settings(uid)
    max_tokens = LENGTHS[s['length']]['tokens']
    kind = "photo" if image_bytes else "text"
    log_event(uid, username, kind, prompt)

    history = get_history(chat_id)
    summary = get_summary(chat_id)
    add_to_history(chat_id, "user", prompt)

    typing = TypingIndicator(chat_id).start()

    placeholder_msg = None
    try:
        placeholder_msg = bot.send_message(chat_id, "…", reply_to_message_id=reply_to)
    except Exception as e:
        print(f"placeholder error: {e}")

    state = {"last_edit": 0.0, "last_len": 0, "text": ""}

    def on_delta(accumulated):
        now = time.time()
        if not placeholder_msg:
            return
        if now - state["last_edit"] < STREAM_EDIT_INTERVAL and len(accumulated) - state["last_len"] < 60:
            return
        shown = accumulated
        if len(shown) > 4000:
            shown = shown[-4000:]
        try:
            bot.edit_message_text(shown, chat_id, placeholder_msg.message_id)
            state["last_edit"] = now
            state["last_len"] = len(accumulated)
            state["text"] = accumulated
        except Exception:
            pass

    try:
        answer, t_in, t_out = ask_gemini(
            prompt, s['model'], max_tokens,
            history=history, image_bytes=image_bytes,
            mime_type=mime_type, summary=summary,
            on_delta=on_delta,
        )

        is_error = answer.startswith("Ошибка:") or answer.startswith("Сервер Gemini")
        if is_error:
            log_event(uid, username, "error", answer[:80])

        if placeholder_msg:
            if is_error or not state["text"]:
                try:
                    bot.edit_message_text(answer[:4000], chat_id, placeholder_msg.message_id)
                except Exception:
                    try:
                        bot.send_message(chat_id, answer)
                    except Exception:
                        pass
            else:
                chunks = split_into_chunks(answer)
                if len(chunks) <= 1:
                    try:
                        bot.edit_message_text(answer[:4000], chat_id, placeholder_msg.message_id)
                    except Exception:
                        pass
                else:
                    try:
                        bot.edit_message_text(chunks[0][:4000], chat_id, placeholder_msg.message_id)
                    except Exception:
                        pass
                    for chunk in chunks[1:]:
                        try:
                            bot.send_chat_action(chat_id, 'typing')
                            bot.send_message(chat_id, chunk)
                            time.sleep(CHUNK_DELAY)
                        except Exception:
                            pass

        if not is_error:
            add_to_history(chat_id, "model", answer.replace(MESSAGE_DELIMITER, " "))

        st = get_stats(uid)
        st['requests'] += 1
        st['tokens_in'] += t_in
        st['tokens_out'] += t_out
        if image_bytes:
            st['images'] += 1

    except Exception as e:
        log_event(uid, username, "error", str(e)[:80])
        try:
            bot.send_message(chat_id, f"Ошибка: {e}")
        except Exception:
            pass
    finally:
        typing.stop()


def rate_limited(uid):
    now = time.time()
    if uid in last_message_time and now - last_message_time[uid] < RATE_LIMIT_SECONDS:
        return RATE_LIMIT_SECONDS - (now - last_message_time[uid])
    last_message_time[uid] = now
    return 0.0


# ============================================================
# ===================== CALLBACK ============================
# ============================================================
@bot.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("menu:"))
def handle_menu_callback(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    try:
        parts = call.data.split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        chat_id = call.message.chat.id
        uid = call.from_user.id
        msg_id = call.message.message_id

        if action == "model" and len(parts) == 2:
            bot.edit_message_text(text_model_list(uid), chat_id, msg_id,
                                  reply_markup=inline_models(uid)); return
        if action == "model" and len(parts) == 3:
            name = parts[2]
            if name in MODELS:
                get_settings(uid)["model"] = name
                bot.edit_message_text(f"✅ Модель: {name}", chat_id, msg_id,
                                      reply_markup=inline_models(uid))
            else:
                bot.edit_message_text("❌ Неизвестная модель.", chat_id, msg_id,
                                      reply_markup=inline_main())
            return
        if action == "style" and len(parts) == 2:
            bot.edit_message_text(text_style_list(uid), chat_id, msg_id,
                                  reply_markup=inline_lengths(uid)); return
        if action == "style" and len(parts) == 3:
            key = parts[2]
            if key in LENGTHS:
                get_settings(uid)["length"] = key
                bot.edit_message_text(f"✅ Стиль: {LENGTHS[key]['label']}", chat_id, msg_id,
                                      reply_markup=inline_lengths(uid))
            else:
                bot.edit_message_text("❌ Неизвестный стиль.", chat_id, msg_id,
                                      reply_markup=inline_main())
            return
        if action == "agent" and len(parts) == 3:
            state = parts[2]
            with agent_lock:
                agent_mode[chat_id] = (state == "on")
            if state == "on":
                os.makedirs(agent_work_path(chat_id), exist_ok=True)
            bot.edit_message_text(text_agent_status(chat_id), chat_id, msg_id,
                                  reply_markup=inline_agent(chat_id))
            return
        if action == "memory":
            n = history_len(chat_id)
            bot.edit_message_text(
                f"╔══════════════════════════╗\n"
                f"║   🧠  ПАМЯТЬ              ║\n"
                f"╚══════════════════════════╝\n\n"
                f"Сообщений в памяти: {n}\n"
                f"Лимит: {HISTORY_LIMIT}\n"
                f"Контекст: {'есть' if get_summary(chat_id) else 'нет'}\n\n"
                f"«Забудь всё» или /clear — сбросить.\n{LINE}",
                chat_id, msg_id, reply_markup=inline_main())
            return
        if action == "back":
            bot.edit_message_text("📋 Меню функций:", chat_id, msg_id,
                                  reply_markup=inline_main()); return

        simple = {
            "help": text_help,
            "reset": text_cleared,
            "example": text_help,
            "photo": text_photo,
            "logs": text_logs,
            "speed": text_speed,
            "about": lambda: text_about(uid),
            "stats": lambda: text_stats(uid),
        }
        fn = simple.get(action)
        if fn is None:
            bot.edit_message_text("❌ Неизвестный пункт.", chat_id, msg_id,
                                  reply_markup=inline_main()); return
        bot.edit_message_text(fn(), chat_id, msg_id, reply_markup=inline_main())
    except Exception as e:
        print(f"callback error: {e}")


# ============================================================
# ===================== КОМАНДЫ =============================
# ============================================================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    uid = message.from_user.id
    s = get_settings(uid)
    hint = "👥 В группе: «Ксай, ...» или reply на моё сообщение.\n" if is_group(message.chat) else ""
    bot.send_message(
        message.chat.id,
        f"╔══════════════════════════╗\n"
        f"║   🤖  {ASSISTANT_NAME}            ║\n"
        f"╚══════════════════════════╝\n\n"
        f"Привет, я {ASSISTANT_NAME} ({ASSISTANT_ALT_NAME}).\n"
        f"Пишу коротко, разбираю фото, помню контекст.\n{hint}\n"
        f"⚙️ Модель: {s['model']}\n"
        f"🎨 Стиль: {LENGTHS[s['length']]['label']}\n"
        f"🧠 Память: {HISTORY_LIMIT} сообщений\n{LINE}",
        reply_markup=main_menu(),
    )


@bot.message_handler(commands=['help'])
def cmd_help(message):
    bot.send_message(message.chat.id, text_help(), reply_markup=main_menu())


@bot.message_handler(commands=['clear'])
def cmd_clear(message):
    clear_history(message.chat.id)
    bot.send_message(message.chat.id, text_cleared(), reply_markup=main_menu())


@bot.message_handler(commands=['menu'])
def cmd_menu(message):
    bot.send_message(message.chat.id, "📋 Меню функций:", reply_markup=inline_main())


@bot.message_handler(commands=['model'])
def cmd_model(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id, text_model_list(uid), reply_markup=inline_models(uid))


@bot.message_handler(commands=['style'])
def cmd_style(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id, text_style_list(uid), reply_markup=inline_lengths(uid))


@bot.message_handler(commands=['agent'])
def cmd_agent(message):
    chat_id = message.chat.id
    with agent_lock:
        enabled = agent_mode.get(chat_id, False)
    if enabled:
        agent_mode[chat_id] = False
    else:
        agent_mode[chat_id] = True
        os.makedirs(agent_work_path(chat_id), exist_ok=True)
    bot.send_message(chat_id, text_agent_status(chat_id), reply_markup=inline_agent(chat_id))


@bot.message_handler(commands=['stat', 'stats'])
def cmd_stats(message):
    bot.send_message(message.chat.id, text_stats(message.from_user.id), reply_markup=main_menu())


@bot.message_handler(commands=['who'])
def cmd_who(message):
    bot.send_message(message.chat.id, text_who(message.from_user.id), reply_markup=main_menu())


@bot.message_handler(commands=['photo'])
def cmd_photo(message):
    bot.send_message(message.chat.id, text_photo(), reply_markup=main_menu())


@bot.message_handler(commands=['log', 'logs'])
def cmd_log(message):
    bot.send_message(message.chat.id, text_logs(), reply_markup=main_menu())


@bot.message_handler(commands=['author'])
def cmd_author(message):
    bot.send_message(message.chat.id, AUTHOR_ANSWER, reply_markup=main_menu())


# ============================================================
# ================= REPLY-КНОПКИ ============================
# ============================================================
@bot.message_handler(func=lambda m: getattr(m, 'text', None) in MAIN_BUTTONS)
def handle_main_button(message):
    t = message.text
    uid = message.from_user.id
    if t == "📋 Меню": cmd_menu(message)
    elif t == "⚙️ Модель": cmd_model(message)
    elif t == "🎨 Стиль": cmd_style(message)
    elif t == "📖 Помощь": cmd_help(message)
    elif t == "🖼 Фото":
        photo = user_last_photo.get(uid)
        if photo:
            left = rate_limited(uid)
            if left > 0:
                bot.send_message(message.chat.id, f"⏳ Подожди {left:.1f} сек."); return
            do_ask(message.chat.id, uid, DEFAULT_IMAGE_PROMPT,
                   reply_to=message.message_id,
                   image_bytes=photo["bytes"], mime_type=photo["mime"],
                   username=user_tag(message.from_user))
        else:
            bot.send_message(message.chat.id, text_photo(), reply_markup=main_menu())
    elif t == "💡 Пример": bot.send_message(message.chat.id, text_help(), reply_markup=main_menu())
    elif t == "ℹ️ О боте": bot.send_message(message.chat.id, text_about(uid), reply_markup=main_menu())


# ============================================================
# ================== ФОТО ===================================
# ============================================================
def _handle_image_message(message, file_id, mime, caption_text):
    uid = message.from_user.id
    in_group = is_group(message.chat)
    replied_to_bot = is_reply_to_bot(message)
    addressed = (not in_group) or has_trigger(caption_text) or replied_to_bot

    try:
        image_bytes = download_telegram_file(file_id)
    except Exception as e:
        if addressed:
            bot.reply_to(message, f"❌ Не удалось скачать фото: {e}")
            log_event(uid, user_tag(message.from_user), "error", f"download: {e}")
        return

    if len(image_bytes) > MAX_IMAGE_BYTES:
        if addressed:
            bot.reply_to(message, "❌ Файл слишком большой.")
        return

    user_last_photo[uid] = {"bytes": image_bytes, "mime": mime}

    if not addressed:
        return

    left = rate_limited(uid)
    if left > 0:
        bot.reply_to(message, f"⏳ Подожди {left:.1f} сек."); return

    prompt = strip_trigger(caption_text) if caption_text else ""
    if not prompt:
        prompt = DEFAULT_IMAGE_PROMPT

    do_ask(message.chat.id, uid, prompt,
           reply_to=message.message_id,
           image_bytes=image_bytes, mime_type=mime,
           username=user_tag(message.from_user))


@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    photo = message.photo[-1]
    if photo.file_size and photo.file_size > MAX_IMAGE_BYTES:
        if is_group(message.chat) and not (has_trigger(message.caption or "") or is_reply_to_bot(message)):
            return
        bot.reply_to(message, f"❌ Файл больше {MAX_IMAGE_BYTES // (1024 * 1024)} МБ."); return
    _handle_image_message(message, photo.file_id, "image/jpeg", message.caption or "")


@bot.message_handler(content_types=['document'])
def handle_document(message):
    doc = message.document
    if not doc:
        return
    mime = (doc.mime_type or "").lower()
    caption = message.caption or ""
    fname = (doc.file_name or "").lower()

    if mime in ("application/zip", "application/x-zip-compressed") or fname.endswith(".zip"):
        with agent_lock:
            enabled = agent_mode.get(message.chat.id, False)
        if enabled:
            left = rate_limited(message.from_user.id)
            if left > 0:
                bot.reply_to(message, f"⏳ Подожди {left:.1f} сек."); return
            try:
                data = download_telegram_file(doc.file_id)
                if len(data) > MAX_ARCHIVE_BYTES:
                    bot.reply_to(message, "❌ Архив слишком большой."); return
                result = agent_extract_zip(message.chat.id, data, doc.file_name or "")
                if isinstance(result, str):
                    bot.reply_to(message, f"❌ {result}"); return
                lines = [f"📦 Архив распакован: {len(result)} файлов", ""]
                for name, size in result[:30]:
                    lines.append(f" ▸ {name} ({size} б)")
                if len(result) > 30:
                    lines.append(f" ... и ещё {len(result) - 30}")
                bot.reply_to(message, "\n".join(lines))
                log_event(message.from_user.id, user_tag(message.from_user),
                          "agent", f"ZIP: {doc.file_name}")
            except Exception as e:
                bot.reply_to(message, f"❌ Ошибка: {e}")
            return

    text_exts = (".txt", ".py", ".json", ".csv", ".md", ".log", ".xml", ".yaml", ".yml")
    if fname.endswith(text_exts):
        with agent_lock:
            enabled = agent_mode.get(message.chat.id, False)
        if enabled:
            try:
                data = download_telegram_file(doc.file_id)
                text = agent_read_text_file(data, doc.file_name or "")
                preview = text[:1500] + ("\n..." if len(text) > 1500 else "")
                bot.reply_to(message, f"📄 {doc.file_name}:\n\n{preview}")
                log_event(message.from_user.id, user_tag(message.from_user),
                          "agent", f"файл: {doc.file_name}")
            except Exception as e:
                bot.reply_to(message, f"❌ Ошибка: {e}")
            return

    if not mime.startswith("image/"):
        return
    if doc.file_size and doc.file_size > MAX_IMAGE_BYTES:
        if is_group(message.chat) and not (has_trigger(caption) or is_reply_to_bot(message)):
            return
        bot.reply_to(message, f"❌ Файл больше {MAX_IMAGE_BYTES // (1024 * 1024)} МБ."); return
    _handle_image_message(message, doc.file_id, mime, caption)


# ============================================================
# ================== ТЕКСТ ==================================
# ============================================================
@bot.message_handler(
    content_types=['text'],
    func=lambda m: (not is_command(m)) and (getattr(m, 'text', None) not in MAIN_BUTTONS),
)
def handle_message(message):
    uid = message.from_user.id
    text = message.text or ""
    in_group = is_group(message.chat)
    replied_to_bot = is_reply_to_bot(message)

    if in_group:
        update_group_knowledge(message.chat, message.from_user)

    if in_group and not has_trigger(text) and not replied_to_bot:
        return

    cleaned = strip_trigger(text) if in_group else text.strip()

    if not cleaned:
        bot.reply_to(message, "Я на месте. Что нужно?")
        return

    # ====== АВТООБНОВЛЕНИЕ (только для разработчика) ======
    if uid == DEVELOPER_ID and is_update_request(text):
        perform_update(message)
        return

    left = rate_limited(uid)
    if left > 0:
        bot.reply_to(message, f"⏳ Подожди {left:.1f} сек."); return

    if is_clear_request(cleaned):
        clear_history(message.chat.id)
        log_event(uid, user_tag(message.from_user), "text", "[clear]")
        bot.send_message(message.chat.id, text_cleared(), reply_markup=main_menu()); return

    if is_author_request(cleaned):
        log_event(uid, user_tag(message.from_user), "text", cleaned)
        bot.send_message(message.chat.id, AUTHOR_ANSWER, reply_markup=main_menu()); return

    if is_log_request(cleaned):
        bot.send_message(message.chat.id, text_logs(), reply_markup=main_menu()); return

    if is_agent_request(cleaned):
        with agent_lock:
            enabled = agent_mode.get(message.chat.id, False)
        if enabled:
            agent_mode[message.chat.id] = False
        else:
            agent_mode[message.chat.id] = True
            os.makedirs(agent_work_path(message.chat.id), exist_ok=True)
        bot.send_message(message.chat.id, text_agent_status(message.chat.id),
                         reply_markup=inline_agent(message.chat.id))
        return

    if is_photo_request(cleaned):
        photo = user_last_photo.get(uid)
        if photo:
            do_ask(message.chat.id, uid, cleaned,
                   reply_to=message.message_id,
                   image_bytes=photo["bytes"], mime_type=photo["mime"],
                   username=user_tag(message.from_user))
            return
        bot.reply_to(message, "📸 Сначала пришли фото, потом расскажу, что на нём."); return

    final_prompt = cleaned
    if in_group:
        ctx = get_group_context(message.chat.id)
        if ctx:
            final_prompt = f"{cleaned}\n\n[Контекст группы: {ctx}]"

    do_ask(message.chat.id, uid, final_prompt,
           reply_to=message.message_id,
           username=user_tag(message.from_user))


# ============================================================
# ================== ЗАПУСК =================================
# ============================================================
if __name__ == "__main__":
    print("╔══════════════════════════════════════════╗")
    print("║  Бот запущен                             ║")
    print(f"║  Помощник: {ASSISTANT_NAME} ({ASSISTANT_ALT_NAME})")
    print(f"║  Автор: {AUTHOR_NAME} ({AUTHOR_HANDLE})")
    print("╚══════════════════════════════════════════╝")
    print(f"🔑 Ключей Gemini: {len(GEMINI_API_KEYS)}")
    print(f"📦 Агент: {AGENT_WORK_DIR}")
    print(f"🧠 Память: {HISTORY_LIMIT} сообщений + краткий контекст")
    print(f"⚡ Стриминг: SSE, правка сообщения раз в {STREAM_EDIT_INTERVAL}с")
    print(f"⚙️ Модель по умолчанию: {DEFAULT_MODEL}")
    print(f"🔄 Автообновление: git pull из {REPO_URL} (ветка {REPO_BRANCH})")
    print(f"👤 Разработчик (ID для 'Ксай обновись'): {DEVELOPER_ID}")
    print(f"📁 REPO_PATH: {REPO_PATH}")
    print()
    print("⚠️  ВАЖНО: в BotFather → /setprivacy → Disable, иначе бот не видит")
    print("    обычные сообщения в группах и триггер «Ксай» не срабатывает.")
    print()

    get_bot_id()

    try:
        bot.set_my_commands([
            types.BotCommand("start", "🚀 Запустить"),
            types.BotCommand("menu", "📋 Меню функций"),
            types.BotCommand("model", "⚙️ Выбор модели"),
            types.BotCommand("style", "🎨 Стиль ответа"),
            types.BotCommand("agent", "🤖 Режим агента"),
            types.BotCommand("photo", "🖼 Как работать с фото"),
            types.BotCommand("clear", "🧹 Сбросить память"),
            types.BotCommand("log", "📋 Последние логи"),
            types.BotCommand("stat", "📊 Статистика"),
            types.BotCommand("who", "🤖 Кто я"),
            types.BotCommand("author", "👤 Автор бота"),
            types.BotCommand("help", "📖 Справка"),
        ])
        print("✅ Команды зарегистрированы.")
    except Exception as e:
        print(f"⚠️ set_my_commands: {e}")

    bot.infinity_polling(timeout=15, long_polling_timeout=8)
