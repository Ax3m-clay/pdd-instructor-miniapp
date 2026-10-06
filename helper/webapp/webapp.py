"""Telegram Mini App для бота автошколы.

Отдельный HTTP-listener внутри процесса autoschool_helper: наружу (через
Tailscale Funnel) публикуется только он, а служебные эндпоинты хелпера с
X-Autoschool-Token остаются на локальном порту.

Авторизация — подпись Telegram WebApp initData ботовым токеном; дополнительно
user.id сверяется с settings.chat_id, чтобы апку открыл только владелец.
Владельцем становится первый, кто открыл апку после чистой установки.
"""

import hashlib
import hmac
import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

STATIC_DIR = Path(__file__).resolve().parent / "static"
INIT_DATA_TTL = 24 * 3600


class AuthError(Exception):
    pass


def check_init_data(init_data, bot_token):
    """Проверить подпись initData. Возвращает user dict."""
    if not init_data:
        raise AuthError("no_init_data")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    if not received:
        raise AuthError("no_hash")
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise AuthError("bad_signature")
    try:
        auth_date = int(pairs.get("auth_date") or 0)
    except ValueError:
        auth_date = 0
    if not auth_date or time.time() - auth_date > INIT_DATA_TTL:
        raise AuthError("expired")
    try:
        return json.loads(pairs.get("user") or "{}")
    except json.JSONDecodeError:
        raise AuthError("bad_user")


def make_handler(deps):
    bot_token = deps["TELEGRAM_TOKEN"]
    start_id = deps["LESSON_START_ID"]

    def display_number(lesson):
        # Номер внутри модуля приходит с платформы; для старых записей считаем сами.
        return int(lesson.get("number") or 0) or (int(lesson.get("lesson_number") or 0) - start_id + 1)

    def public_lesson(lesson, with_summary=False):
        item = {
            "id": lesson.get("id"),
            "number": display_number(lesson),
            "lesson_number": lesson.get("lesson_number"),
            "title": lesson.get("title") or "",
            "status": lesson.get("status"),
            "completed_at": lesson.get("completed_at") or "",
            "module_number": str(lesson.get("module_number") or ""),
            "module_title": lesson.get("module_title") or "",
            "has_summary": bool(lesson.get("summary")),
            # None — ещё не проверяли; False — теста на платформе нет.
            "has_test": lesson.get("has_test"),
        }
        if with_summary:
            item["summary"] = lesson.get("summary") or ""
        return item

    def find_lesson(state, lesson_id):
        return next((x for x in state["lessons"] if x.get("id") == lesson_id), None)

    def build_summary(lesson_id):
        """Скачать урок, получить выжимку, сохранить её в state. Долгая операция."""
        with deps["LOCK"]:
            state = deps["load_state"]()
            lesson = find_lesson(state, lesson_id)
        if not lesson:
            return None, "lesson_not_found"
        text = deps["clean_text"](deps["fetch_lesson_html"](lesson["url"]))
        valid, reason = deps["lesson_quality"](text)
        if not valid:
            return None, reason
        test_text, has_test = "", None
        try:
            test = deps["fetch_lesson_test"](int(lesson["lesson_number"]))
            has_test = bool(test)
            test_text = deps["format_test_for_llm"](test)
        except Exception:
            pass
        summary = deps["openai_summary"](text, test_text)
        with deps["LOCK"]:
            state = deps["load_state"]()
            stored = find_lesson(state, lesson_id)
            if stored is not None:
                stored["summary"] = summary
                stored["summary_at"] = deps["now_moscow_iso"]()
                if has_test is not None:
                    stored["has_test"] = has_test
                deps["save_state"](state)
        return summary, ""

    class Handler(BaseHTTPRequestHandler):
        server_version = "AutoschoolWebApp/1.0"

        def log_message(self, fmt, *args):
            # Временно пишем обращения: надо видеть, доходит ли телефон до сервера.
            ua = (self.headers.get("User-Agent") or "?")[:60]
            ip = self.headers.get("X-Forwarded-For") or self.client_address[0]
            print(f"[webapp] {ip} {fmt % args} ua={ua}", flush=True)

        # --- инфраструктура ответа ---

        def _send_json(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, name, content_type):
            path = STATIC_DIR / name
            if not path.exists():
                self._send_json(404, {"ok": False, "error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                return {}

        def _user(self):
            """Авторизовать запрос. Бросает AuthError."""
            init_data = self.headers.get("X-Telegram-Init-Data") or ""
            user = check_init_data(init_data, bot_token)
            user_id = str(user.get("id") or "")
            with deps["LOCK"]:
                state = deps["load_state"]()
                settings = state.setdefault("settings", {})
                owner = str(settings.get("chat_id") or "")
                if not owner and user_id:
                    settings["chat_id"] = owner = user_id
                    deps["save_state"](state)
                    print(f"[webapp] владелец мини-апки: {user_id}", flush=True)
            if user_id != owner:
                raise AuthError("forbidden")
            return user

        # --- маршруты ---

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send_file("index.html", "text/html; charset=utf-8")
                return
            if path == "/healthz":
                self._send_json(200, {"ok": True})
                return
            self._api(path, "GET")

        def do_POST(self):
            self._api(urlparse(self.path).path, "POST")

        def _api(self, path, method):
            if not path.startswith("/api/"):
                self._send_json(404, {"ok": False, "error": "not_found"})
                return
            try:
                self._user()
            except AuthError as e:
                self._send_json(401, {"ok": False, "error": str(e)})
                return
            try:
                self._route(path, method)
            except Exception as e:
                traceback.print_exc()
                self._send_json(500, {"ok": False, "error": str(e)})

        def _route(self, path, method):
            query = dict(parse_qsl(urlparse(self.path).query))

            if path == "/api/state" and method == "GET":
                # Урок могли пройти на сайте — подтягиваем статусы перед показом.
                try:
                    deps["sync_platform"]()
                except Exception:
                    pass
                with deps["LOCK"]:
                    state = deps["load_state"]()
                lessons = sorted(
                    state["lessons"], key=lambda x: int(x.get("lesson_number") or 0)
                )
                done = [x for x in lessons if x.get("status") == "done"]
                self._send_json(200, {
                    "ok": True,
                    "total": len(lessons),
                    "done": len(done),
                    "lessons": [public_lesson(x) for x in lessons],
                })
                return

            if path == "/api/lesson" and method == "GET":
                lesson_id = query.get("id") or ""
                with deps["LOCK"]:
                    state = deps["load_state"]()
                    lesson = find_lesson(state, lesson_id)
                if not lesson:
                    self._send_json(404, {"ok": False, "error": "lesson_not_found"})
                    return
                if not lesson.get("summary"):
                    summary, err = build_summary(lesson_id)
                    if err:
                        self._send_json(200, {"ok": False, "error": err,
                                              "lesson": public_lesson(lesson)})
                        return
                    lesson = dict(lesson, summary=summary)
                self._send_json(200, {"ok": True, "lesson": public_lesson(lesson, True)})
                return

            if path == "/api/today" and method == "POST":
                deps["discover_lessons"](force=False)
                with deps["LOCK"]:
                    state = deps["load_state"]()
                    pending = sorted(
                        [x for x in state["lessons"] if x.get("status") == "pending"],
                        key=lambda x: int(x.get("lesson_number") or 0),
                    )
                if not pending:
                    # Новый урок мог открыться после сегодняшнего сканирования.
                    deps["discover_lessons"](force=True)
                    with deps["LOCK"]:
                        state = deps["load_state"]()
                        pending = sorted(
                            [x for x in state["lessons"] if x.get("status") == "pending"],
                            key=lambda x: int(x.get("lesson_number") or 0),
                        )
                if not pending:
                    with deps["LOCK"]:
                        state = deps["load_state"]()
                        waiting = [public_lesson(x) for x in state["lessons"]
                                   if x.get("status") == "read"]
                    self._send_json(200, {"ok": True, "no_lessons": True, "waiting_test": waiting})
                    return
                lesson = pending[0]
                summary = lesson.get("summary")
                if not summary:
                    summary, err = build_summary(lesson["id"])
                    if err:
                        self._send_json(200, {"ok": False, "error": err,
                                              "lesson": public_lesson(lesson)})
                        return
                lesson = dict(lesson, summary=summary)
                self._send_json(200, {"ok": True, "lesson": public_lesson(lesson, True)})
                return

            if path == "/api/complete" and method == "POST":
                payload = self._read_json()
                lesson_id = str(payload.get("id") or "")
                with deps["LOCK"]:
                    state = deps["load_state"]()
                    lesson = find_lesson(state, lesson_id)
                if not lesson:
                    self._send_json(404, {"ok": False, "error": "lesson_not_found"})
                    return
                deps["complete_lesson_on_platform"](int(lesson["lesson_number"]))
                try:
                    deps["sync_platform"](force=True)
                except Exception:
                    pass
                # Статус берём с платформы: урок станет «пройден», когда тест
                # на сайте сдан руками, до этого он «прочитан».
                with deps["LOCK"]:
                    state = deps["load_state"]()
                    lesson = find_lesson(state, lesson_id)
                self._send_json(200, {"ok": True, "lesson": public_lesson(lesson) if lesson else None})
                return

            if path == "/api/chat" and method == "GET":
                self._send_json(200, {"ok": True, "messages": deps["chat_history"](50)})
                return

            if path == "/api/chat" and method == "POST":
                payload = self._read_json()
                question = str(payload.get("message") or "").strip()[:2000]
                if not question:
                    self._send_json(400, {"ok": False, "error": "empty_message"})
                    return
                lesson_id = str(payload.get("lesson_id") or "") or None
                lesson = None
                if lesson_id:
                    with deps["LOCK"]:
                        lesson = find_lesson(deps["load_state"](), lesson_id)
                answer = deps["openai_chat"](question, lesson)
                # В историю пишем только удачную пару вопрос-ответ.
                deps["chat_add"]("user", question, lesson_id)
                deps["chat_add"]("assistant", answer, lesson_id)
                self._send_json(200, {"ok": True, "answer": answer})
                return

            if path == "/api/chat/clear" and method == "POST":
                deps["chat_clear"]()
                self._send_json(200, {"ok": True})
                return

            self._send_json(404, {"ok": False, "error": "not_found"})

    return Handler


def start(host, port, deps):
    """Поднять мини-апку в фоновом потоке. Возвращает сервер."""
    server = ThreadingHTTPServer((host, port), make_handler(deps))
    thread = threading.Thread(target=server.serve_forever, name="webapp", daemon=True)
    thread.start()
    return server
