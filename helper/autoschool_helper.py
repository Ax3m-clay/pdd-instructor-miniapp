#!/usr/bin/env python3
import html
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import threading
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


BASE_DIR = Path(os.environ.get("AUTOSCHOOL_DIR", "/home/llm/autoschool"))
STATE_FILE = BASE_DIR / "state.json"  # старое хранилище, при первом старте переносится в DB_FILE
DB_FILE = BASE_DIR / "autoschool.db"
COOKIE_JAR = BASE_DIR / "proffskills.cookies"
COOKIE_FILE = BASE_DIR / "proffskills.cookie"
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://ru.cheapvibecode.ru/v1").rstrip("/")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "deepseek-v4-flash")
HELPER_TOKEN = os.environ["AUTOSCHOOL_HELPER_TOKEN"]
SOCKS_PROXY = os.environ.get("AUTOSCHOOL_SOCKS_PROXY", "127.0.0.1:10808")
REFERRAL_URL = os.environ.get("PROFFSKILLS_REFERRAL_URL", "")
LISTEN_HOST = os.environ.get("AUTOSCHOOL_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("AUTOSCHOOL_LISTEN_PORT", "8787"))
LESSON_START_ID = int(os.environ.get("LESSON_START_ID", "24"))
MAX_SCAN_GAP = int(os.environ.get("MAX_SCAN_GAP", "5"))
MAX_SCAN_LESSONS = int(os.environ.get("MAX_SCAN_LESSONS", "300"))
RATE_LIMIT_MINUTES = int(os.environ.get("RATE_LIMIT_MINUTES", "10"))
# Статусы урока на стороне Proffskills: закрыт, доступен, прочитан, пройден.
PLATFORM_LOCKED, PLATFORM_OPEN, PLATFORM_READ, PLATFORM_DONE = 0, 1, 2, 3
PLATFORM_STATUS = {
    PLATFORM_LOCKED: "locked",
    PLATFORM_OPEN: "pending",
    PLATFORM_READ: "read",   # конспект отмечен прочитанным, но тест не сдан
    PLATFORM_DONE: "done",
}
SYNC_TTL_SECONDS = int(os.environ.get("SYNC_TTL_SECONDS", "60"))
# Границы промпта: вход к модели — основная статья расхода токенов.
SUMMARY_TEXT_LIMIT = int(os.environ.get("SUMMARY_TEXT_LIMIT", "8000"))
SUMMARY_TEST_LIMIT = int(os.environ.get("SUMMARY_TEST_LIMIT", "6000"))
OPENAI_MAX_TOKENS = int(os.environ.get("OPENAI_MAX_TOKENS", "1500"))
WEBAPP_ENABLED = os.environ.get("WEBAPP_ENABLED", "1") not in ("0", "false", "no")
WEBAPP_HOST = os.environ.get("WEBAPP_HOST", "127.0.0.1")
WEBAPP_PORT = int(os.environ.get("WEBAPP_PORT", "8790"))
# Публичный HTTPS-адрес мини-апки: на старте ставим боту кнопку меню «Open» на него.
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip()
# Сессия по реферальной ссылке живёт 14 дней, обновляем с большим запасом.
REFERRAL_REFRESH_SECONDS = int(os.environ.get("REFERRAL_REFRESH_SECONDS", str(6 * 3600)))
CHAT_CONTEXT_MESSAGES = int(os.environ.get("CHAT_CONTEXT_MESSAGES", "12"))
CHAT_MAX_TOKENS = int(os.environ.get("CHAT_MAX_TOKENS", "800"))

BASE_DIR.mkdir(parents=True, exist_ok=True)
LOCK = threading.RLock()
SERVER = None


def today_moscow():
    if ZoneInfo:
        return datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


def now_moscow_iso():
    if ZoneInfo:
        return datetime.now(ZoneInfo("Europe/Moscow")).isoformat()
    return datetime.now().isoformat()


def default_state():
    return {
        "telegram_offset": 0,
        "last_discover_at": "",
        "settings": {
            "id": "main",
            "chat_id": "",
            "last_lesson_date": "",
            "remind_time": "20:00",
        },
        "lessons": [],
    }


# --- хранилище: SQLite ---
# Отдельного сервера БД нет: это файл в томе /data, память почти не тратится.
# load_state/save_state сохраняют прежний интерфейс «весь state словарём»,
# поэтому остальной код не знает, JSON там или SQL.

LESSON_COLUMNS = (
    "id", "lesson_number", "number", "module_id", "module_number", "module_title",
    "title", "url", "status", "completed_at", "summary", "summary_at", "has_test",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id            TEXT PRIMARY KEY,
    lesson_number INTEGER NOT NULL,
    number        INTEGER,
    module_id     INTEGER,
    module_number TEXT,
    module_title  TEXT,
    title         TEXT,
    url           TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    completed_at  TEXT,
    summary       TEXT,
    summary_at    TEXT,
    has_test      INTEGER,
    extra         TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    lesson_id  TEXT,
    created_at TEXT NOT NULL
);
"""


def db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    os.chmod(DB_FILE, 0o600)
    migrate_json_state()


def migrate_json_state():
    """Один раз перенести старый state.json в базу и убрать его с дороги."""
    if not STATE_FILE.exists():
        return
    with closing(db()) as conn:
        empty = conn.execute("SELECT NOT EXISTS (SELECT 1 FROM lessons)").fetchone()[0]
    if empty:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            save_state(json.load(f))
        print("state.json перенесён в SQLite", flush=True)
    STATE_FILE.rename(STATE_FILE.with_name("state.json.migrated"))


def load_state():
    state = default_state()
    with closing(db()) as conn:
        for row in conn.execute("SELECT key, value FROM kv"):
            state[row["key"]] = json.loads(row["value"])
        lessons = []
        for row in conn.execute("SELECT * FROM lessons ORDER BY lesson_number"):
            # NULL-колонки не кладём в словарь: для кода это «ключа нет», как было в JSON.
            lesson = {c: row[c] for c in LESSON_COLUMNS if row[c] is not None}
            if "has_test" in lesson:
                lesson["has_test"] = bool(lesson["has_test"])
            lesson.update(json.loads(row["extra"] or "{}"))
            lessons.append(lesson)
    state["lessons"] = lessons
    return state


def save_state(state):
    rows = []
    for lesson in state.get("lessons") or []:
        row = {c: lesson.get(c) for c in LESSON_COLUMNS}
        if row["has_test"] is not None:
            row["has_test"] = int(bool(row["has_test"]))
        row["extra"] = json.dumps(
            {k: v for k, v in lesson.items() if k not in LESSON_COLUMNS}, ensure_ascii=False
        )
        rows.append(row)
    columns = LESSON_COLUMNS + ("extra",)
    upsert = (
        f"INSERT INTO lessons ({', '.join(columns)}) "
        f"VALUES ({', '.join(':' + c for c in columns)}) "
        f"ON CONFLICT(id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in columns[1:])}"
    )
    with closing(db()) as conn, conn:  # второй with — транзакция: всё или ничего
        conn.executemany(upsert, rows)
        keep = {r["id"] for r in rows}
        stale = [(r["id"],) for r in conn.execute("SELECT id FROM lessons") if r["id"] not in keep]
        conn.executemany("DELETE FROM lessons WHERE id = ?", stale)
        conn.executemany(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v, ensure_ascii=False)) for k, v in state.items() if k != "lessons"],
        )


def chat_history(limit=50):
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT role, content, lesson_id, created_at FROM chat_messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def chat_add(role, content, lesson_id=None):
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO chat_messages (role, content, lesson_id, created_at) VALUES (?, ?, ?, ?)",
            (role, content, lesson_id, now_moscow_iso()),
        )


def chat_clear():
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM chat_messages")


def run_curl(args, timeout=45):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "curl failed")
    return result.stdout


def refresh_referral_session(force=False):
    """Войти по реферальной ссылке: Proffskills кладёт sessionid и csrftoken в cookie jar."""
    if not REFERRAL_URL:
        return
    fresh = COOKIE_JAR.exists() and time.time() - COOKIE_JAR.stat().st_mtime < REFERRAL_REFRESH_SECONDS
    if fresh and not force:
        return
    run_curl(
        ["curl", "-sS", "-L", "-m", "30", "-A", "Mozilla/5.0",
         "-c", str(COOKIE_JAR), "-b", str(COOKIE_JAR), REFERRAL_URL, "-o", "/dev/null"],
        timeout=40,
    )


def jar_cookie_header():
    """Cookie jar curl'а (формат Netscape) → строка для заголовка Cookie."""
    if not COOKIE_JAR.exists():
        return ""
    pairs = []
    for line in COOKIE_JAR.read_text(encoding="utf-8").splitlines():
        if line.startswith("#HttpOnly_"):  # у HttpOnly-кук (sessionid) такой префикс, это не комментарий
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == 7:
            pairs.append(f"{parts[5]}={parts[6]}")
    return "; ".join(pairs)


def get_cookie_value():
    # Кука, положенная руками, главнее; иначе — сессия по реферальной ссылке.
    if COOKIE_FILE.exists():
        return COOKIE_FILE.read_text(encoding="utf-8").strip().replace("\n", " ").replace("\r", " ")
    try:
        refresh_referral_session()
    except Exception as e:
        print(f"referral login failed: {e}", flush=True)
    return jar_cookie_header()


def get_csrf_token():
    cookie = get_cookie_value()
    m = re.search(r"csrftoken=([^;\s]+)", cookie)
    return m.group(1) if m else ""


def proffskills_api(method, endpoint, body=None, timeout=45):
    """Make authenticated API call to Proffskills (direct, no proxy)."""
    cookie = get_cookie_value()
    csrf = get_csrf_token()
    args = [
        "curl",
        "-sS",
        "-L",
        "-m",
        str(timeout - 5),
        "-A",
        "Mozilla/5.0",
        "-H",
        f"Cookie: {cookie}",
        "-H",
        "Content-Type: application/json",
    ]
    if csrf:
        args.extend(["-H", f"X-CSRFToken: {csrf}"])
    args.extend(["-X", method])
    if body is not None:
        args.extend(["-d", json.dumps(body, ensure_ascii=False)])
    args.append(f"https://proffskills.ru{endpoint}")
    raw = run_curl(args, timeout=timeout)
    if not raw.strip():
        return {}
    return json.loads(raw)


def telegram_api(method, payload):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    raw = run_curl(
        [
            "curl",
            "--socks5-hostname",
            SOCKS_PROXY,
            "-sS",
            "-m",
            "40",
            "-H",
            "Content-Type: application/json",
            "-X",
            "POST",
            "-d",
            json.dumps(payload, ensure_ascii=False),
            url,
        ],
        timeout=50,
    )
    data = json.loads(raw)
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data.get('description', 'unknown')}")
    return data.get("result")


def fetch_lesson_test(lesson_number):
    """Fetch lesson test questions from Proffskills API."""
    data = proffskills_api("GET", f"/api/v3/autoschool_theory/test/?lesson_id={lesson_number}", timeout=45)
    return data.get("test")


def format_test_for_llm(test, budget=None):
    """Выборка вопросов теста для промпта, ужатая в бюджет символов.

    Тесты бывают на сотню с лишним вопросов; целиком они стоят дороже самого
    конспекта, а для выжимки хватает части. Правильные ответы в промпт не
    передаются: вопросы нужны только чтобы понять, на чём делать акцент.
    """
    if not test:
        return ""
    budget = SUMMARY_TEST_LIMIT if budget is None else budget
    questions = test.get("questions", [])
    blocks, used = [], 0
    for q in questions:
        block = "\nВопрос: " + (q.get("question") or "")
        for ans in q.get("answers", []):
            block += "\n  - " + (ans.get("answer") or "")
        if used + len(block) > budget:
            break
        used += len(block)
        blocks.append(block)
    if not blocks:
        return ""
    head = f"Выборка вопросов из теста ({len(blocks)} из {len(questions)}):"
    return head + "".join(blocks)


def complete_lesson_on_platform(lesson_number):
    """Отметить урок пройденным на Proffskills без сдачи теста."""
    return proffskills_api(
        "PATCH",
        f"/api/v3/autoschool_theory/lessons/?lesson_id={lesson_number}",
        body={"is_completed": True},
        timeout=45,
    )


def openai_summary(lesson_text, test_text=""):
    lesson_text = (lesson_text or "")[:SUMMARY_TEXT_LIMIT]
    test_text = (test_text or "")[:SUMMARY_TEST_LIMIT]
    combined = lesson_text
    if test_text:
        combined += f"\n\n---\n\n{test_text}"

    prompt = f"""Пользователь не любит читать длинные конспекты и часто откладывает обучение. Преврати этот урок автошколы в короткое занятие на 5 минут.

Важно: ниже приведён не только конспект урока, но и вопросы из теста к нему (без ответов). Используй их, чтобы понять, на что делать акцент: объясни темы, о которых спрашивают, так, чтобы человек сам смог ответить.

Материал урока и тест:

{combined}

Сделай ответ строго в таком формате, обычным текстом без markdown-разметки:

🚗 Урок: [определи тему]

Кратко за 5 минут:
[8-10 простых предложений, основанных на конспекте и тесте]

Что обязательно запомнить:
1.
2.
3.
4.
5.

Типичные ошибки новичков:
1.
2.
3.

Мини-тест:
1. Вопрос с вариантами А/Б/В
2. Вопрос с вариантами А/Б/В
3. Вопрос с вариантами А/Б/В
4. Вопрос с вариантами А/Б/В
5. Вопрос с вариантами А/Б/В

Ответы:
Правильные ответы с короткими объяснениями.

Главный вывод:
Одна короткая фраза.

Если в тексте нет урока, а только страница авторизации или мусор, напиши: Не удалось получить текст урока, нужна авторизация/cookie."""
    return openai_complete(
        [
            {
                "role": "system",
                "content": "Ты помощник ученика автошколы в России. Объясняй ПДД просто, кратко и понятно. Не растягивай ответ. Цель — помочь пользователю заниматься без прокрастинации. Опирайся на тестовые вопросы, чтобы подсветить самое важное.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
        max_tokens=OPENAI_MAX_TOKENS,
    )


TUTOR_PROMPT = """Ты — инструктор автошколы в России. Помогаешь ученику понять ПДД и сдать теорию в ГАИ.
Отвечай по действующим ПДД РФ: коротко, простыми словами, без длинных вступлений.
Если помогает — приведи пример из реальной дороги. Если вопрос про конкретное правило — назови пункт ПДД.
Если не уверен — так и скажи и посоветуй свериться с текстом ПДД. Не используй таблицы."""


def openai_chat(question, lesson=None):
    """Ответ инструктора с учётом последних сообщений и, если есть, текущего урока."""
    system = TUTOR_PROMPT
    if lesson and lesson.get("summary"):
        system += (
            f"\n\nСейчас ученик проходит урок «{lesson.get('title') or lesson.get('id')}». "
            f"Его конспект:\n{lesson['summary'][:4000]}"
        )
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in chat_history(CHAT_CONTEXT_MESSAGES)
    ]
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": question}]
    return openai_complete(messages, temperature=0.5, max_tokens=CHAT_MAX_TOKENS)


def openai_complete(messages, temperature, max_tokens):
    payload = {
        "model": OPENAI_MODEL,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    raw = run_curl(
        [
            "curl",
            "-sS",
            "-m",
            "90",
            "-H",
            f"Authorization: Bearer {OPENAI_API_KEY}",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(payload, ensure_ascii=False),
            f"{OPENAI_BASE_URL}/chat/completions",
        ],
        timeout=100,
    )
    data = json.loads(raw)
    if "error" in data:
        raise RuntimeError(f"OpenAI API error: {data['error'].get('message', 'unknown')}")
    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    if not content:
        raise RuntimeError("Модель вернула пустой ответ")
    return content


def clean_text(raw):
    text = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<noscript[\s\S]*?</noscript>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:30000]


def fetch_lesson_html(url):
    cookie_value = get_cookie_value()

    if cookie_value:
        return run_curl(
            [
                "curl",
                "-sS",
                "-L",
                "-m",
                "40",
                "-A",
                "Mozilla/5.0",
                "-H",
                f"Cookie: {cookie_value}",
                url,
            ],
            timeout=50,
        )

    if REFERRAL_URL:
        run_curl(
            [
                "curl",
                "-sS",
                "-L",
                "-m",
                "30",
                "-A",
                "Mozilla/5.0",
                "-c",
                str(COOKIE_JAR),
                "-b",
                str(COOKIE_JAR),
                REFERRAL_URL,
                "-o",
                "/dev/null",
            ],
            timeout=40,
        )
    return run_curl(
        [
            "curl",
            "-sS",
            "-L",
            "-m",
            "40",
            "-A",
            "Mozilla/5.0",
            "-b",
            str(COOKIE_JAR),
            url,
        ],
        timeout=50,
    )


def lesson_quality(text):
    if len(text) < 1000:
        return False, "too_short"
    if re.search(r"Авторизация|Войти|Логин|Пароль", text, flags=re.I):
        return False, "auth_required"
    if re.search(r"Требуется подписание договора", text, flags=re.I):
        return False, "contract_required"
    return True, "ok"


def extract_title(raw_html):
    """Достать заголовок урока из <h2>5 Урок. Название</h2>."""
    m = re.search(r"(?is)<h2[^>]*>\s*(?:\d+\s*Урок\.\s*)?(.{3,150}?)</h2>", raw_html)
    if not m:
        return ""
    title = html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
    return re.sub(r"\s+", " ", title).strip()


def is_lesson_accessible(lesson_number):
    url = f"https://proffskills.ru/autoschool_theory/lesson/{lesson_number}/summary/"
    try:
        html_text = fetch_lesson_html(url)
        text = clean_text(html_text)
        valid, reason = lesson_quality(text)
        return valid, text, reason, extract_title(html_text)
    except Exception as e:
        return False, "", str(e), ""


def platform_catalog():
    """Уроки всех доступных модулей Proffskills: номер урока -> данные."""
    data = proffskills_api("GET", "/api/v3/autoschool_theory/modules/", timeout=45)
    catalog = {}
    for module in data.get("modules") or []:
        module_id = module.get("id")
        if not module_id:
            continue
        try:
            listing = proffskills_api(
                "GET", f"/api/v3/autoschool_theory/lessons/list/?module_id={module_id}", timeout=45
            )
        except Exception:
            continue  # закрытый или недоступный модуль просто пропускаем
        for item in listing.get("lessons") or []:
            try:
                number = int(item["id"])
            except (KeyError, TypeError, ValueError):
                continue
            catalog[number] = {
                "title": (item.get("title") or "").strip(),
                "number": int(item.get("number") or 0),
                "platform_status": int(item.get("status") or 0),
                "module_id": module_id,
                "module_number": str(module.get("module_number") or ""),
                "module_title": (module.get("title") or "").strip(),
            }
    return catalog


def sync_platform(force=False):
    """Подтянуть список уроков и их статусы с платформы.

    Урок могли пройти на сайте мимо бота, поэтому статус платформы главнее
    локального. Возвращает число новых уроков или None, если синк не делался.
    """
    with LOCK:
        state = load_state()
        last_ts = float(state.get("last_sync_ts") or 0)
    if not force and time.time() - last_ts < SYNC_TTL_SECONDS:
        return None

    catalog = platform_catalog()
    if not catalog:
        return None

    added = 0
    with LOCK:
        state = load_state()
        existing = {int(x.get("lesson_number") or 0): x for x in state["lessons"]}
        for number, info in sorted(catalog.items()):
            lesson = existing.get(number)
            if lesson is None:
                lesson = {
                    "id": f"lesson_{number}",
                    "lesson_number": number,
                    "url": f"https://proffskills.ru/autoschool_theory/lesson/{number}/summary/",
                    "status": "locked" if info["platform_status"] == PLATFORM_LOCKED else "pending",
                    "completed_at": "",
                }
                state["lessons"].append(lesson)
                existing[number] = lesson
                added += 1
            if info["title"]:
                lesson["title"] = info["title"]
            lesson["number"] = info["number"]
            lesson["module_id"] = info["module_id"]
            lesson["module_number"] = info["module_number"]
            lesson["module_title"] = info["module_title"]
            status = PLATFORM_STATUS.get(info["platform_status"], "pending")
            lesson["status"] = status
            if status == "done":
                lesson["completed_at"] = lesson.get("completed_at") or now_moscow_iso()
            else:
                lesson["completed_at"] = "" 
        state["lessons"].sort(key=lambda x: int(x.get("lesson_number") or 0))
        state["last_sync_ts"] = time.time()
        save_state(state)
    return added


def discover_lessons(force=False):
    try:
        added = sync_platform(force=force)
        if added is not None:
            with LOCK:
                state = load_state()
                state["last_discover_at"] = now_moscow_iso()
                save_state(state)
                return {
                    "discovered": added,
                    "total": len(state["lessons"]),
                    "cached": False,
                    "source": "api",
                }
    except Exception as e:
        print(f"sync_platform failed, fallback to page scan: {e}", flush=True)

    with LOCK:
        state = load_state()
        last_discover = state.get("last_discover_at", "")
        if not force and last_discover and last_discover.startswith(today_moscow()):
            return {"discovered": 0, "total": len(state["lessons"]), "cached": True}

        existing_numbers = {int(x.get("lesson_number", 0)) for x in state["lessons"]}
        discovered = []
        gap = 0
        lesson_number = LESSON_START_ID
        max_lesson = max(existing_numbers or {LESSON_START_ID})
        scan_until = max(max_lesson + MAX_SCAN_GAP + 5, LESSON_START_ID + MAX_SCAN_LESSONS)

        while lesson_number <= scan_until and gap < MAX_SCAN_GAP:
            valid, text, reason, page_title = is_lesson_accessible(lesson_number)
            if valid:
                title = page_title or f"Урок {lesson_number}"
                if not page_title:
                    sentences = re.split(r"(?<=[.!?])\s+", text[:400])
                    for sent in sentences[:2]:
                        if re.search(r"[Уу]рок\s*\d+|Общие положения|ПДД|понятия|дорожного движения", sent, re.I):
                            title = sent.strip()[:150]
                            break
                existing = next((x for x in state["lessons"] if int(x.get("lesson_number", 0)) == lesson_number), None)
                if not existing:
                    discovered.append({
                        "id": f"lesson_{lesson_number}",
                        "lesson_number": lesson_number,
                        "title": title,
                        "url": f"https://proffskills.ru/autoschool_theory/lesson/{lesson_number}/summary/",
                        "status": "pending",
                        "completed_at": "",
                    })
                elif page_title and (
                    force or re.fullmatch(r"Урок \d+", str(existing.get("title") or ""))
                ):
                    existing["title"] = title
                gap = 0
            else:
                gap += 1
            lesson_number += 1

        if discovered:
            existing_map = {x["id"]: x for x in state["lessons"]}
            for lesson in discovered:
                if lesson["id"] not in existing_map:
                    state["lessons"].append(lesson)
            state["lessons"].sort(key=lambda x: int(x.get("lesson_number") or 0))
        state["last_discover_at"] = now_moscow_iso()
        save_state(state)

        return {
            "discovered": len(discovered),
            "total": len(state["lessons"]),
            "cached": False,
            "last_checked": lesson_number - 1,
        }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _authorized(self):
        if self.path == "/health":
            return True
        return self.headers.get("X-Autoschool-Token") == HELPER_TOKEN

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        if not self._authorized():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path == "/health":
                self._send(200, {"ok": True})
                return
            if parsed.path == "/telegram/updates":
                with LOCK:
                    state = load_state()
                    offset = int(state.get("telegram_offset") or 0)
                result = telegram_api(
                    "getUpdates",
                    {"offset": offset, "timeout": 0, "allowed_updates": ["message"]},
                )
                updates = []
                max_update_id = offset - 1
                for item in result:
                    max_update_id = max(max_update_id, int(item.get("update_id", 0)))
                    msg = item.get("message") or {}
                    chat = msg.get("chat") or {}
                    text = msg.get("text") or ""
                    if chat.get("id") and text:
                        updates.append(
                            {
                                "update_id": item.get("update_id"),
                                "chat_id": str(chat.get("id")),
                                "text": text,
                            }
                        )
                with LOCK:
                    state = load_state()
                    if max_update_id >= offset:
                        state["telegram_offset"] = max_update_id + 1
                    save_state(state)
                self._send(200, {"ok": True, "updates": updates})
                return
            if parsed.path == "/lessons/discover":
                force = (qs.get("force") or [""])[0].lower() in ("1", "true", "yes")
                result = discover_lessons(force=force)
                self._send(200, {"ok": True, **result})
                return
            if parsed.path == "/lessons/next":
                chat_id = (qs.get("chat_id") or [""])[0]
                discover_lessons(force=False)
                with LOCK:
                    state = load_state()
                    if chat_id:
                        state["settings"]["chat_id"] = chat_id
                    pending = sorted(
                        [x for x in state["lessons"] if x.get("status") == "pending"],
                        key=lambda x: int(x.get("lesson_number") or 0),
                    )
                    save_state(state)
                if not pending:
                    # Новый урок мог открыться уже после сегодняшнего сканирования
                    # (например, сразу после сдачи предыдущего теста) - пересканируем.
                    discover_lessons(force=True)
                    with LOCK:
                        state = load_state()
                        pending = sorted(
                            [x for x in state["lessons"] if x.get("status") == "pending"],
                            key=lambda x: int(x.get("lesson_number") or 0),
                        )
                if not pending:
                    self._send(200, {"ok": True, "chat_id": chat_id, "no_lessons": True})
                    return
                self._send(200, {"ok": True, "chat_id": chat_id, "lesson": pending[0]})
                return
            if parsed.path == "/lessons/fetch":
                lesson_id = (qs.get("id") or [""])[0]
                chat_id = (qs.get("chat_id") or [""])[0]
                with LOCK:
                    state = load_state()
                    lesson = next((x for x in state["lessons"] if x.get("id") == lesson_id), None)
                if not lesson:
                    self._send(404, {"ok": False, "error": "lesson_not_found"})
                    return
                text = clean_text(fetch_lesson_html(lesson["url"]))
                valid, reason = lesson_quality(text)

                # Fetch test for this lesson
                test_text = ""
                try:
                    test = fetch_lesson_test(int(lesson["lesson_number"]))
                    test_text = format_test_for_llm(test)
                except Exception:
                    pass

                self._send(
                    200,
                    {
                        "ok": True,
                        "chat_id": chat_id,
                        "lesson": lesson,
                        "lesson_text": text,
                        "test_text": test_text,
                        "valid": valid,
                        "reason": reason,
                    },
                )
                return
            if parsed.path == "/lessons/test":
                lesson_id = (qs.get("id") or [""])[0]
                with LOCK:
                    state = load_state()
                    lesson = next((x for x in state["lessons"] if x.get("id") == lesson_id), None)
                if not lesson:
                    self._send(404, {"ok": False, "error": "lesson_not_found"})
                    return
                test = fetch_lesson_test(int(lesson["lesson_number"]))
                self._send(200, {"ok": True, "lesson_id": lesson_id, "test": test})
                return
            if parsed.path == "/progress":
                with LOCK:
                    state = load_state()
                total = len(state["lessons"])
                done = len([x for x in state["lessons"] if x.get("status") == "done"])
                self._send(200, {"ok": True, "total": total, "done": done, "lessons": state["lessons"]})
                return
            if parsed.path == "/reminder/due":
                with LOCK:
                    state = load_state()
                settings = state.get("settings", {})
                chat_id = settings.get("chat_id") or ""
                due = bool(chat_id) and settings.get("last_lesson_date") != today_moscow()
                self._send(200, {"ok": True, "due": due, "chat_id": chat_id})
                return
            self._send(404, {"ok": False, "error": "not_found"})
        except Exception as e:
            self._send(500, {"ok": False, "error": str(e)})

    def do_POST(self):
        if not self._authorized():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            parsed = urlparse(self.path)
            payload = self._read_json()
            if parsed.path == "/telegram/send":
                chat_id = str(payload.get("chat_id") or "")
                text = str(payload.get("text") or "")
                if not chat_id or not text:
                    self._send(400, {"ok": False, "error": "chat_id_and_text_required"})
                    return
                chunks = []
                while len(text) > 4000:
                    chunks.append(text[:4000])
                    text = text[4000:]
                if text:
                    chunks.append(text)
                last_result = None
                for chunk in chunks:
                    last_result = telegram_api("sendMessage", {"chat_id": chat_id, "text": chunk})
                self._send(200, {"ok": True, "result": last_result, "chunks": len(chunks)})
                return
            if parsed.path == "/openai/summary":
                lesson_text = str(payload.get("lesson_text") or "")
                test_text = str(payload.get("test_text") or "")
                chat_id = str(payload.get("chat_id") or "")
                lesson_id = str(payload.get("lesson_id") or payload.get("id") or "")
                valid, reason = lesson_quality(lesson_text)
                if not valid:
                    self._send(400, {"ok": False, "error": reason})
                    return
                result = openai_summary(lesson_text, test_text)
                if lesson_id:
                    with LOCK:
                        state = load_state()
                        lesson = next((x for x in state["lessons"] if x.get("id") == lesson_id), None)
                        if lesson is not None:
                            lesson["summary"] = result
                            lesson["summary_at"] = now_moscow_iso()
                            save_state(state)
                self._send(200, {"ok": True, "chat_id": chat_id, "lesson_id": lesson_id, "result": result})
                return
            if parsed.path == "/lessons/complete":
                lesson_id = str(payload.get("id") or "")
                with LOCK:
                    state = load_state()
                    lesson = next((x for x in state["lessons"] if x.get("id") == lesson_id), None)
                    if not lesson:
                        self._send(404, {"ok": False, "error": "lesson_not_found"})
                        return
                    lesson["status"] = "done"
                    lesson["completed_at"] = now_moscow_iso()
                    state["settings"]["last_lesson_date"] = today_moscow()
                    save_state(state)
                self._send(200, {"ok": True, "lesson": lesson})
                return
            if parsed.path == "/lessons/reset":
                with LOCK:
                    state = load_state()
                    for lesson in state["lessons"]:
                        lesson["status"] = "pending"
                        lesson["completed_at"] = ""
                    state["settings"]["last_lesson_date"] = ""
                    save_state(state)
                self._send(200, {"ok": True})
                return
            self._send(404, {"ok": False, "error": "not_found"})
        except Exception as e:
            self._send(500, {"ok": False, "error": str(e)})


def start_webapp():
    """Поднять Telegram Mini App на отдельном (публикуемом) порту."""
    if not WEBAPP_ENABLED:
        return
    sys.path.insert(0, str(Path(__file__).parent / "webapp"))
    import webapp

    deps = {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "LESSON_START_ID": LESSON_START_ID,
        "LOCK": LOCK,
        "load_state": load_state,
        "save_state": save_state,
        "clean_text": clean_text,
        "fetch_lesson_html": fetch_lesson_html,
        "lesson_quality": lesson_quality,
        "fetch_lesson_test": fetch_lesson_test,
        "format_test_for_llm": format_test_for_llm,
        "openai_summary": openai_summary,
        "discover_lessons": discover_lessons,
        "sync_platform": sync_platform,
        "complete_lesson_on_platform": complete_lesson_on_platform,
        "now_moscow_iso": now_moscow_iso,
        "openai_chat": openai_chat,
        "chat_history": chat_history,
        "chat_add": chat_add,
        "chat_clear": chat_clear,
    }
    webapp.start(WEBAPP_HOST, WEBAPP_PORT, deps)
    print(f"webapp listening on {WEBAPP_HOST}:{WEBAPP_PORT}", flush=True)


def setup_menu_button():
    """Кнопка «Open» рядом с полем ввода в чате с ботом — открывает мини-апку."""
    if not WEBAPP_URL:
        return
    try:
        telegram_api(
            "setChatMenuButton",
            {"menu_button": {"type": "web_app", "text": "Open", "web_app": {"url": WEBAPP_URL}}},
        )
        print(f"menu button Open -> {WEBAPP_URL}", flush=True)
    except Exception as e:
        print(f"menu button failed: {e}", flush=True)


def shutdown_handler(signum, frame):
    # shutdown() ждёт, пока serve_forever выйдет из цикла, а он крутится в этом же
    # (главном) потоке — вызов напрямую повесил бы процесс. Поэтому из другого потока.
    if SERVER:
        threading.Thread(target=SERVER.shutdown, daemon=True).start()
    else:
        sys.exit(0)  # сигнал пришёл ещё до старта сервера, например во время первого сканирования


def main():
    global SERVER
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    init_db()
    threading.Thread(target=setup_menu_button, daemon=True).start()
    with LOCK:
        state = load_state()
        if not state.get("lessons"):
            discover_lessons(force=True)
    start_webapp()
    SERVER = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    SERVER.serve_forever()


if __name__ == "__main__":
    main()
