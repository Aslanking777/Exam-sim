import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ContentType
from aiogram.filters import Command
from aiogram.types import ErrorEvent
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

import google.generativeai as genai
from PIL import Image
from dotenv import load_dotenv


DB_PATH = os.environ.get("DB_PATH", "greenbook.db")

# Load .env if present (beginner-friendly)
load_dotenv()

BUSY_MESSAGE = "The system is currently busy due to high demand. Please try again in a moment."
PARSE_FAILED_MESSAGE = "I couldn’t read that file clearly. Please try a clearer screenshot or a different PDF page."


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_required(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


PARSE_SECONDS_PER_QUESTION = safe_int(os.environ.get("SECONDS_PER_QUESTION", "90"), 90)


@dataclass
class ParsedQuestion:
    passage: str
    question: str
    options: list[str]
    correct_answer: str
    requires_image_crop: bool
    image_path: str | None


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tests (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              title TEXT,
              status TEXT NOT NULL,
              total_time INTEGER,
              raw_json TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              test_id INTEGER NOT NULL,
              idx INTEGER NOT NULL,
              stem TEXT NOT NULL,
              passage TEXT,
              question TEXT,
              options_json TEXT NOT NULL,
              correct_answer TEXT NOT NULL,
              requires_image_crop INTEGER NOT NULL DEFAULT 0,
              image_path TEXT,
              confidence_score INTEGER NOT NULL DEFAULT 100,
              status TEXT NOT NULL DEFAULT 'ok',
              FOREIGN KEY(test_id) REFERENCES tests(id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS test_files (
              test_id INTEGER PRIMARY KEY,
              filename TEXT,
              mime_type TEXT,
              data BLOB NOT NULL,
              FOREIGN KEY(test_id) REFERENCES tests(id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS progress (
              user_id INTEGER NOT NULL,
              test_id INTEGER NOT NULL,
              updated_at TEXT NOT NULL,
              state_json TEXT NOT NULL,
              PRIMARY KEY (user_id, test_id),
              FOREIGN KEY(test_id) REFERENCES tests(id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS time_spent_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              test_id INTEGER NOT NULL,
              question_id INTEGER NOT NULL,
              seconds_spent INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(test_id) REFERENCES tests(id),
              FOREIGN KEY(question_id) REFERENCES questions(id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              test_id INTEGER,
              created_at TEXT NOT NULL,
              report_json TEXT NOT NULL
            )
            """
        )

        # Lightweight migrations for existing DBs (safe on repeated runs)
        async def _col_exists(table: str, col: str) -> bool:
            rows = await db.execute_fetchall(f"PRAGMA table_info({table})")
            return any(r[1] == col for r in rows)

        if not await _col_exists("tests", "total_time"):
            await db.execute("ALTER TABLE tests ADD COLUMN total_time INTEGER")

        for col, ddl in [
            ("stem", "ALTER TABLE questions ADD COLUMN stem TEXT NOT NULL DEFAULT ''"),
            ("passage", "ALTER TABLE questions ADD COLUMN passage TEXT"),
            ("question", "ALTER TABLE questions ADD COLUMN question TEXT"),
            ("requires_image_crop", "ALTER TABLE questions ADD COLUMN requires_image_crop INTEGER NOT NULL DEFAULT 0"),
            ("image_path", "ALTER TABLE questions ADD COLUMN image_path TEXT"),
        ]:
            if not await _col_exists("questions", col):
                await db.execute(ddl)

        await db.commit()


PARSER_PROMPT = """You are a specialized SAT/IELTS Document Extraction Engine. 
Your sole objective is to analyze cropped images of exam questions and convert them into a machine-readable JSON format for a digital simulation interface.

STRICT OPERATIONAL PROTOCOLS:
1. OUTPUT FORMAT: Respond ONLY with a valid, raw JSON object. 
   - DO NOT use markdown code blocks (no ```json).
   - DO NOT include introductory or concluding remarks.
2. CROP CONTEXT: The input image is a crop. Focus on the central content. Ignore peripheral noise, UI elements, or fragments of adjacent questions.
3. EXTRACTION FIELDS:
   - "passage": The full reading context or setup text. Use \\n for paragraph breaks.

   - "question": The specific question or prompt being asked.
   - "options": An array of exactly 4 strings (A, B, C, D). For grid-in math questions, return an empty array [].
   - "correct_answer": The correct option (A, B, C, or D). If grid-in, return the numerical answer.
   - "requires_image_crop": Set to TRUE if the content contains complex math symbols (fractions, roots), geometry, graphs, tables, or diagrams that cannot be perfectly rendered in plain text. Set to FALSE for standard text-only questions.
4. NO ALTERATIONS: Do not solve the question. Do not summarize. Extract the text exactly as it appears.

JSON SCHEMA:
{
  "questions": [
    {
      "passage": "string",
      "question": "string",
      "options": ["string", "string", "string", "string"],
      "correct_answer": "string",
      "requires_image_crop": boolean
    }
  ]
}"""


def _extract_json(text: str) -> dict[str, Any]:
    """
    The model sometimes wraps JSON in code fences or adds extra text.
    We aggressively extract the first top-level JSON object.
    """
    text = (text or "").strip()
    # Strip common markdown code fences even if the model violates instructions
    text = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```", "", text)

    # Fast path
    try:
        return json.loads(text)
    except Exception:
        pass

    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("No JSON object found in model output.")
    return json.loads(m.group(0))


def _looks_like_429(e: Exception) -> bool:
    s = f"{type(e).__name__}: {e}".lower()
    return ("429" in s) or ("resource_exhausted" in s) or ("quota" in s) or ("too many requests" in s)


async def _with_backoff(coro_factory, *, retries: int = 3, base_delays: list[float] | None = None):
    delays = base_delays or [5.0, 10.0, 15.0]
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return await coro_factory()
        except Exception as e:
            last_exc = e
            if not _looks_like_429(e) or attempt >= retries - 1:
                raise
            await asyncio.sleep(delays[min(attempt, len(delays) - 1)])
    if last_exc:
        raise last_exc


async def model_parse_bytes(filename: str, data: bytes) -> dict[str, Any]:
    api_key = env_required("GEMINI_API_KEY")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")

    lower = filename.lower()
    if lower.endswith(".pdf"):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
            f.write(data)
            tmp_path = f.name
        try:
            async def _run():
                uploaded = genai.upload_file(tmp_path, mime_type="application/pdf")
                resp = model.generate_content([PARSER_PROMPT, uploaded])
                return _extract_json(getattr(resp, "text", "") or "")

            return await _with_backoff(_run, retries=3)
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    else:
        try:
            from io import BytesIO

            img = Image.open(BytesIO(data))

            async def _run():
                resp = model.generate_content([PARSER_PROMPT, img])
                return _extract_json(getattr(resp, "text", "") or "")

            return await _with_backoff(_run, retries=3)
        except Exception:
            # If PIL fails (unknown format), fallback to file upload
            with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1] or ".bin") as f:
                f.write(data)
                tmp_path = f.name
            try:
                async def _run():
                    uploaded = genai.upload_file(tmp_path)
                    resp = model.generate_content([PARSER_PROMPT, uploaded])
                    return _extract_json(getattr(resp, "text", "") or "")

                return await _with_backoff(_run, retries=3)
            finally:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass


def normalize_questions(payload: dict[str, Any]) -> tuple[str | None, list[ParsedQuestion]]:
    title = payload.get("title")
    questions = payload.get("questions", [])
    out: list[ParsedQuestion] = []
    for q in questions:
        passage = str(q.get("passage", "")).strip()
        question = str(q.get("question", "")).strip()
        options = q.get("options", [])
        if not isinstance(options, list):
            options = []
        options = [str(o).strip() for o in options if str(o).strip()]
        correct = str(q.get("correct_answer", "")).strip()
        requires_image_crop = bool(q.get("requires_image_crop", False))

        # Options must be exactly 4 strings, or [] for grid-in.
        if len(options) == 0:
            norm_opts: list[str] = []
        else:
            norm_opts = options[:4]
            if len(norm_opts) != 4:
                norm_opts = []

        if (passage or question) and correct:
            out.append(
                ParsedQuestion(
                    passage=passage,
                    question=question,
                    options=norm_opts,
                    correct_answer=correct,
                    requires_image_crop=requires_image_crop,
                    image_path=None,
                )
            )
    return (str(title).strip() if isinstance(title, str) and title.strip() else None, out)


async def db_insert_test(
    user_id: int,
    title: str | None,
    raw_json: dict[str, Any],
    questions: list[ParsedQuestion],
    *,
    total_time: int,
    uploaded_file: tuple[str, str, bytes] | None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tests (user_id, created_at, title, status, total_time, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, utc_now_iso(), title, "parsed", int(total_time), json.dumps(raw_json, ensure_ascii=False)),
        )
        test_id = cur.lastrowid

        if uploaded_file:
            filename, mime_type, data = uploaded_file
            await db.execute(
                "INSERT OR REPLACE INTO test_files (test_id, filename, mime_type, data) VALUES (?, ?, ?, ?)",
                (test_id, filename, mime_type, data),
            )

        for idx, q in enumerate(questions, start=1):
            stem = (q.passage + ("\n\n" if q.passage and q.question else "") + q.question).strip()
            await db.execute(
                """
                INSERT INTO questions
                  (test_id, idx, stem, passage, question, options_json, correct_answer, requires_image_crop, image_path, confidence_score, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    test_id,
                    idx,
                    stem,
                    q.passage,
                    q.question,
                    json.dumps(q.options, ensure_ascii=False),
                    q.correct_answer,
                    1 if q.requires_image_crop else 0,
                    q.image_path,
                    100,
                    "ok",
                ),
            )
        await db.commit()
        return int(test_id)


async def db_get_test(test_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        test_row = await db.execute_fetchone(
            "SELECT id, user_id, created_at, title, status, total_time FROM tests WHERE id = ?",
            (test_id,),
        )
        if not test_row:
            return None
        has_file = await db.execute_fetchone("SELECT 1 FROM test_files WHERE test_id = ? LIMIT 1", (test_id,))
        q_rows = await db.execute_fetchall(
            """
            SELECT id, idx, stem, passage, question, options_json, correct_answer, requires_image_crop, image_path, confidence_score, status
            FROM questions
            WHERE test_id = ?
            ORDER BY idx ASC
            """,
            (test_id,),
        )
        questions: list[dict[str, Any]] = []
        for (qid, idx, stem, passage, question, options_json, correct, requires_img, image_path, conf, status) in q_rows:
            passage_s = (passage or "").strip()
            question_s = (question or "").strip()
            stem_s = (stem or "").strip()
            if not passage_s and not question_s and stem_s:
                # Backward compatibility: older rows stored everything in stem.
                question_s = stem_s
            questions.append(
                {
                    "id": qid,
                    "idx": idx,
                    "stem": stem_s or (passage_s + ("\n\n" if passage_s and question_s else "") + question_s).strip(),
                    "passage": passage_s,
                    "question": question_s,
                    "options": json.loads(options_json),
                    "correct_answer": correct,
                    "requires_image_crop": bool(requires_img),
                    "image_path": image_path
                    or (f"/api/test_file/{test_id}" if requires_img and has_file else None),
                    "confidence_score": conf,
                    "status": status,
                }
            )
        total_time = safe_int(test_row[5], 0)
        if total_time <= 0:
            total_time = len(questions) * PARSE_SECONDS_PER_QUESTION
        return {
            "id": test_row[0],
            "user_id": test_row[1],
            "created_at": test_row[2],
            "title": test_row[3],
            "status": test_row[4],
            "total_time": total_time,
            "questions": questions,
        }


async def db_save_report(user_id: int, report: dict[str, Any]) -> None:
    test_id = report.get("test_id")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO reports (user_id, test_id, created_at, report_json) VALUES (?, ?, ?, ?)",
            (
                user_id,
                safe_int(test_id) if test_id is not None else None,
                utc_now_iso(),
                json.dumps(report, ensure_ascii=False),
            ),
        )

        # Log per-question time spent
        per_q = report.get("per_question") or report.get("question_times") or []
        if isinstance(per_q, list):
            for item in per_q:
                if not isinstance(item, dict):
                    continue
                qid = safe_int(item.get("question_id"))
                sec = safe_int(item.get("seconds_spent"))
                if qid > 0 and sec >= 0:
                    await db.execute(
                        """
                        INSERT INTO time_spent_log
                          (user_id, test_id, question_id, seconds_spent, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (user_id, safe_int(test_id), qid, sec, utc_now_iso()),
                    )
        await db.commit()


def build_webapp_kb(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Open Greenbook Web App", web_app=WebAppInfo(url=url))]
        ]
    )


async def cmd_start(message: Message) -> None:
    webapp_url = os.environ.get("WEBAPP_URL", "").strip() or "https://example.com/"
    await message.answer(
        "Send me a SAT PDF or an image screenshot of questions, and I’ll build a test you can take in the Web App.",
        reply_markup=build_webapp_kb(webapp_url),
    )


async def _run_parsing_status(bot: Bot, chat_id: int, message_id: int, start_ts: float) -> None:
    while True:
        elapsed = max(0, int(asyncio.get_running_loop().time() - start_ts))
        mm = elapsed // 60
        ss = elapsed % 60
        now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"Parsing... {mm:02d}:{ss:02d} ({now_utc})",
            )
        except Exception:
            # Editing can fail if the message is unchanged/too frequent/etc.
            pass
        await asyncio.sleep(2)


async def handle_upload(message: Message, bot: Bot) -> None:
    doc = message.document
    photo = message.photo[-1] if message.photo else None

    if not doc and not photo:
        return

    filename = "upload"
    file_id = None
    if doc:
        filename = doc.file_name or "upload.pdf"
        file_id = doc.file_id
    elif photo:
        filename = "upload.jpg"
        file_id = photo.file_id

    status = await message.answer("Parsing...")
    start_ts = asyncio.get_running_loop().time()
    status_task = asyncio.create_task(_run_parsing_status(bot, status.chat.id, status.message_id, start_ts))

    tg_file = await bot.get_file(file_id)
    data = await bot.download_file(tg_file.file_path)
    raw_bytes = data.read()

    try:
        parsed = await model_parse_bytes(filename, raw_bytes)
        title, questions = normalize_questions(parsed)
        if not questions:
            raise ValueError("No questions parsed.")
    except Exception as e:
        if _looks_like_429(e):
            await message.answer(BUSY_MESSAGE)
        else:
            await message.answer(PARSE_FAILED_MESSAGE)
        return
    finally:
        status_task.cancel()
        try:
            await status.delete()
        except Exception:
            pass

    total_time = len(questions) * PARSE_SECONDS_PER_QUESTION

    lower = filename.lower()
    uploaded_file: tuple[str, str, bytes] | None = None
    if lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")) or photo:
        mime = "image/jpeg"
        if lower.endswith(".png"):
            mime = "image/png"
        elif lower.endswith(".webp"):
            mime = "image/webp"
        elif lower.endswith(".gif"):
            mime = "image/gif"
        uploaded_file = (filename, mime, raw_bytes)

    test_id = await db_insert_test(
        message.from_user.id,
        title,
        parsed,
        questions,
        total_time=total_time,
        uploaded_file=uploaded_file,
    )

    public_api = os.environ.get("PUBLIC_API_BASE", "").strip()
    webapp = os.environ.get("WEBAPP_URL", "").strip() or "https://example.com/"
    if public_api:
        url = f"{webapp}?test_id={test_id}&api={public_api}&total_time={total_time}"
    else:
        # The web app needs an API base to fetch the test.
        url = f"{webapp}?test_id={test_id}&total_time={total_time}"

    mm = total_time // 60
    ss = total_time % 60
    await message.answer(
        f"Ready: {len(questions)} questions.\nTime: {mm:02d}:{ss:02d}.",
        reply_markup=build_webapp_kb(url),
    )


async def handle_webapp_data(message: Message) -> None:
    """
    Telegram sends web_app_data as a message payload.
    """
    payload = message.web_app_data.data if message.web_app_data else ""
    try:
        report = json.loads(payload)
    except Exception:
        await message.answer("Got Web App data, but it wasn't valid JSON.")
        return

    await db_save_report(message.from_user.id, report)
    total = report.get("total_score")
    await message.answer(f"Report received. Total score: {total}")


async def api_get_test(request: web.Request) -> web.Response:
    test_id = safe_int(request.match_info.get("test_id"))
    test = await db_get_test(test_id)
    if not test:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response(test)


async def api_get_test_file(request: web.Request) -> web.Response:
    test_id = safe_int(request.match_info.get("test_id"))
    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchone(
            "SELECT filename, mime_type, data FROM test_files WHERE test_id = ?",
            (test_id,),
        )
        if not row:
            return web.Response(status=404, text="not_found")
        filename, mime_type, data = row
        headers = {"Content-Disposition": f'inline; filename="{filename or "crop"}"'}
        return web.Response(body=data, content_type=(mime_type or "application/octet-stream"), headers=headers)


async def api_health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "time": utc_now_iso()})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", api_health)
    app.router.add_get("/api/test/{test_id}", api_get_test)
    app.router.add_get("/api/test_file/{test_id}", api_get_test_file)

    # Very permissive CORS for quick GitHub Pages testing
    async def add_cors_headers(request: web.Request, response: web.StreamResponse) -> web.StreamResponse:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        return response

    @web.middleware
    async def cors_mw(request: web.Request, handler):
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
            return await add_cors_headers(request, resp)
        resp = await handler(request)
        return await add_cors_headers(request, resp)

    app.middlewares.append(cors_mw)
    return app


async def run_bot_and_api() -> None:
    await init_db()

    bot = Bot(token=env_required("BOT_TOKEN"))
    dp = Dispatcher()

    async def global_error_handler(event: ErrorEvent) -> None:
        # Never leak technical details to the user.
        try:
            update = event.update
            msg = getattr(update, "message", None) or getattr(update, "callback_query", None) and update.callback_query.message
            if msg:
                await bot.send_message(chat_id=msg.chat.id, text=BUSY_MESSAGE)
        except Exception:
            pass

    dp.errors.register(global_error_handler)

    dp.message.register(cmd_start, Command("start"))
    dp.message.register(handle_webapp_data, F.content_type == ContentType.WEB_APP_DATA)
    dp.message.register(handle_upload, F.document | F.photo)

    host = os.environ.get("HOST", "0.0.0.0")
    port = safe_int(os.environ.get("PORT", 8080), 8080)

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    print(f"API listening on http://{host}:{port}")
    print("Bot polling started…")

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run_bot_and_api())

