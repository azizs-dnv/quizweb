from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parent
QUIZ_DIR = BASE_DIR / "tests" / "quizzes"
AUTH_FILE = BASE_DIR / "auth.json"
DB_FILE = BASE_DIR / "quizroom.db"
COOKIE_NAME = "quizroom_teacher"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
QUIZ_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="QuizRoom", version="1.0.0")


class LoginBody(BaseModel):
    username: str
    password: str


class QuizBody(BaseModel):
    title: str
    duration_minutes: int
    questions: list[dict[str, Any]]


class JoinBody(BaseModel):
    code: str
    name: str


class AnswerBody(BaseModel):
    token: str
    question_id: str
    option_index: int


class TokenBody(BaseModel):
    token: str


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_FILE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


@contextmanager
def _database():
    connection = _connect()
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _init_database() -> None:
    with _database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS rooms (
                code TEXT PRIMARY KEY,
                quiz_id TEXT NOT NULL,
                quiz_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                started_at REAL,
                deadline REAL
            );
            CREATE TABLE IF NOT EXISTS participants (
                token TEXT PRIMARY KEY,
                room_code TEXT NOT NULL,
                name TEXT NOT NULL,
                question_order TEXT NOT NULL,
                option_orders TEXT NOT NULL,
                answers TEXT NOT NULL DEFAULT '{}',
                score INTEGER NOT NULL DEFAULT 0,
                correct_count INTEGER NOT NULL DEFAULT 0,
                warnings INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'waiting',
                joined_at REAL NOT NULL,
                last_seen REAL NOT NULL,
                finished_at REAL,
                FOREIGN KEY(room_code) REFERENCES rooms(code)
            );
            """
        )


_init_database()


def _read_auth() -> dict[str, str] | None:
    if not AUTH_FILE.exists():
        return None
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        if all(key in data for key in ("username", "password_salt", "password_hash", "token_secret")):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _sign_session(secret: str, issued_at: int | None = None) -> str:
    payload = f"teacher:{issued_at or int(time.time())}"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _valid_teacher_token(token: str | None) -> bool:
    auth = _read_auth()
    if not auth or not token or "." not in token:
        return False
    encoded, signature = token.rsplit(".", 1)
    expected = hmac.new(auth["token_secret"].encode(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        kind, issued_at = payload.split(":", 1)
        age = int(time.time()) - int(issued_at)
        return kind == "teacher" and 0 <= age <= SESSION_MAX_AGE
    except (ValueError, UnicodeDecodeError):
        return False


def _require_teacher(request: Request) -> None:
    if not _valid_teacher_token(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Teacher sign-in is required.")


def _quiz_path(quiz_id: str) -> Path:
    if not quiz_id.isalnum():
        raise HTTPException(status_code=404, detail="Quiz not found.")
    return QUIZ_DIR / f"{quiz_id}.json"


def _load_quiz(quiz_id: str) -> dict[str, Any]:
    path = _quiz_path(quiz_id)
    try:
        quiz = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(quiz.get("questions"), list):
            raise ValueError("Invalid quiz")
        return quiz
    except (OSError, json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=404, detail="Quiz not found.") from None


def _public_quiz(quiz: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in quiz.items() if key != "questions"} | {
        "question_count": len(quiz["questions"])
    }


def _room_row(code: str) -> sqlite3.Row:
    with _database() as connection:
        row = connection.execute("SELECT * FROM rooms WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Room not found.")
    return row


def _refresh_room(code: str) -> None:
    with _database() as connection:
        room = connection.execute("SELECT * FROM rooms WHERE code = ?", (code,)).fetchone()
        if room and room["status"] == "running" and room["deadline"] <= time.time():
            connection.execute("UPDATE rooms SET status = 'finished' WHERE code = ?", (code,))
            connection.execute(
                "UPDATE participants SET status = 'completed', finished_at = COALESCE(finished_at, ?) "
                "WHERE room_code = ? AND status != 'blocked'",
                (time.time(), code),
            )


def _ranked_participants(code: str) -> list[dict[str, Any]]:
    with _database() as connection:
        rows = connection.execute(
            "SELECT * FROM participants WHERE room_code = ? ORDER BY score DESC, correct_count DESC, joined_at ASC",
            (code,),
        ).fetchall()
        room = connection.execute("SELECT quiz_json FROM rooms WHERE code = ?", (code,)).fetchone()
    question_count = len(json.loads(room["quiz_json"])["questions"]) if room else 0
    return [
        {
            "rank": rank,
            "name": row["name"],
            "score": row["score"],
            "correct_count": row["correct_count"],
            "question_count": question_count,
            "grade": round(row["correct_count"] / question_count * 10, 1) if question_count else 0,
            "warnings": row["warnings"],
            "status": "disconnected" if row["status"] == "in_progress" and time.time() - row["last_seen"] > 15 else row["status"],
            "answered_count": len(json.loads(row["answers"])),
            "joined_at": row["joined_at"],
            "finished_at": row["finished_at"],
        }
        for rank, row in enumerate(rows, start=1)
    ]


def _room_state(code: str, token: str | None = None) -> dict[str, Any]:
    _refresh_room(code)
    room = _room_row(code)
    quiz = json.loads(room["quiz_json"])
    board = _ranked_participants(code)
    result: dict[str, Any] = {
        "code": code,
        "status": room["status"],
        "quiz": _public_quiz(quiz),
        "deadline": room["deadline"],
        "server_time": time.time(),
        "leaderboard": board,
    }
    if token is None:
        result["participants"] = board
        return result

    with _database() as connection:
        student = connection.execute(
            "SELECT * FROM participants WHERE token = ? AND room_code = ?", (token, code)
        ).fetchone()
        if student is None:
            raise HTTPException(status_code=404, detail="Participant not found.")
        connection.execute("UPDATE participants SET last_seen = ? WHERE token = ?", (time.time(), token))
        ranked_tokens = connection.execute(
            "SELECT token FROM participants WHERE room_code = ? ORDER BY score DESC, correct_count DESC, joined_at ASC",
            (code,),
        ).fetchall()

    order = json.loads(student["question_order"])
    option_orders = json.loads(student["option_orders"])
    answers = json.loads(student["answers"])
    result["student"] = {
        "name": student["name"],
        "token": token,
        "status": student["status"],
        "answers": list(answers),
        "score": student["score"],
        "correct_count": student["correct_count"],
        "warnings": student["warnings"],
        "rank": next((index for index, row in enumerate(ranked_tokens, start=1) if row["token"] == token), None),
    }
    if room["status"] == "running" and student["status"] == "in_progress":
        ordered_questions = []
        questions_by_id = {question["id"]: question for question in quiz["questions"]}
        for question_id in order:
            question = questions_by_id[question_id]
            options = option_orders[question_id]
            ordered_questions.append(
                {
                    "id": question_id,
                    "text": question["text"],
                    "options": [question["options"][index] for index in options],
                    "answered": question_id in answers,
                }
            )
        result["questions"] = ordered_questions
    return result


@app.on_event("startup")
def startup() -> None:
    _init_database()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "index.html")


app.mount("/css", StaticFiles(directory=BASE_DIR / "css"), name="css")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/api/config")
def config() -> dict[str, Any]:
    return {"admin_ready": _read_auth() is not None}


@app.post("/api/auth/login")
def login(body: LoginBody):
    auth = _read_auth()
    if not auth:
        raise HTTPException(status_code=409, detail="Run init_admin.py to create the teacher account first.")
    candidate = hashlib.scrypt(
        body.password.encode(), salt=bytes.fromhex(auth["password_salt"]), n=2**14, r=8, p=1, dklen=32
    ).hex()
    if body.username != auth["username"] or not hmac.compare_digest(candidate, auth["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect username or password.")
    token = _sign_session(auth["token_secret"])
    response = {"username": auth["username"], "token": token}
    from fastapi.responses import JSONResponse

    result = JSONResponse(response)
    result.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=SESSION_MAX_AGE)
    return result


@app.post("/api/auth/logout")
def logout():
    from fastapi.responses import JSONResponse

    result = JSONResponse({"ok": True})
    result.delete_cookie(COOKIE_NAME)
    return result


@app.get("/api/teacher/quizzes")
def list_quizzes(request: Request) -> list[dict[str, Any]]:
    _require_teacher(request)
    quizzes = []
    for path in sorted(QUIZ_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            quiz = json.loads(path.read_text(encoding="utf-8"))
            quizzes.append(_public_quiz(quiz))
        except (OSError, json.JSONDecodeError):
            continue
    return quizzes


@app.post("/api/teacher/quizzes", status_code=201)
def create_quiz(body: QuizBody, request: Request) -> dict[str, Any]:
    _require_teacher(request)
    title = body.title.strip()
    if not title or len(title) > 120:
        raise HTTPException(status_code=422, detail="Quiz title must be between 1 and 120 characters.")
    if not 1 <= body.duration_minutes <= 240:
        raise HTTPException(status_code=422, detail="Duration must be between 1 and 240 minutes.")
    if not 1 <= len(body.questions) <= 100:
        raise HTTPException(status_code=422, detail="Add between 1 and 100 questions.")
    questions = []
    for position, item in enumerate(body.questions, start=1):
        text = str(item.get("text", "")).strip()
        options = [str(option).strip() for option in item.get("options", [])]
        correct_index = item.get("correct_index")
        if not text or len(text) > 1000 or not 2 <= len(options) <= 12 or any(not option for option in options):
            raise HTTPException(status_code=422, detail=f"Check the question text and answer options for question {position}.")
        if not isinstance(correct_index, int) or not 0 <= correct_index < len(options):
            raise HTTPException(status_code=422, detail=f"Select the correct answer for question {position}.")
        questions.append({"id": secrets.token_hex(6), "text": text, "options": options, "correct_index": correct_index})
    quiz = {
        "id": secrets.token_hex(8),
        "title": title,
        "duration_minutes": body.duration_minutes,
        "questions": questions,
        "created_at": time.time(),
    }
    _quiz_path(quiz["id"]).write_text(json.dumps(quiz, ensure_ascii=False, indent=2), encoding="utf-8")
    return _public_quiz(quiz)


@app.delete("/api/teacher/quizzes/{quiz_id}")
def delete_quiz(quiz_id: str, request: Request) -> dict[str, bool]:
    _require_teacher(request)
    path = _quiz_path(quiz_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Quiz not found.")
    with _database() as connection:
        active = connection.execute(
            "SELECT 1 FROM rooms WHERE quiz_id = ? AND status IN ('waiting', 'running') LIMIT 1", (quiz_id,)
        ).fetchone()
    if active:
        raise HTTPException(status_code=409, detail="Finish the active room for this quiz before deleting it.")
    path.unlink()
    return {"ok": True}


@app.get("/api/teacher/rooms")
def list_rooms(request: Request) -> list[dict[str, Any]]:
    _require_teacher(request)
    with _database() as connection:
        rows = connection.execute("SELECT code FROM rooms ORDER BY created_at DESC LIMIT 30").fetchall()
    return [_room_state(row["code"]) for row in rows]


@app.post("/api/teacher/rooms/{quiz_id}", status_code=201)
def create_room(quiz_id: str, request: Request) -> dict[str, Any]:
    _require_teacher(request)
    quiz = _load_quiz(quiz_id)
    with _database() as connection:
        for _ in range(20):
            code = f"{secrets.randbelow(900000) + 100000}"
            try:
                connection.execute(
                    "INSERT INTO rooms(code, quiz_id, quiz_json, status, created_at) VALUES (?, ?, ?, 'waiting', ?)",
                    (code, quiz_id, json.dumps(quiz, ensure_ascii=False), time.time()),
                )
                break
            except sqlite3.IntegrityError:
                continue
        else:
            raise HTTPException(status_code=503, detail="Could not generate a room code.")
    return _room_state(code)


@app.post("/api/student/join", status_code=201)
def join_room(body: JoinBody) -> dict[str, Any]:
    code = body.code.strip()
    name = " ".join(body.name.split())
    if not name or len(name) > 60:
        raise HTTPException(status_code=422, detail="Enter a name (up to 60 characters).")
    room = _room_row(code)
    if room["status"] != "waiting":
        raise HTTPException(status_code=409, detail="This room is no longer accepting participants.")
    quiz = json.loads(room["quiz_json"])
    questions = quiz["questions"]
    question_order = [question["id"] for question in questions]
    random.SystemRandom().shuffle(question_order)
    option_orders = {}
    for question in questions:
        indices = list(range(len(question["options"])))
        random.SystemRandom().shuffle(indices)
        option_orders[question["id"]] = indices
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _database() as connection:
        connection.execute(
            "INSERT INTO participants(token, room_code, name, question_order, option_orders, joined_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token, code, name, json.dumps(question_order), json.dumps(option_orders), now, now),
        )
    return _room_state(code, token)


@app.post("/api/student/resume")
def resume_student(body: TokenBody) -> dict[str, Any]:
    with _database() as connection:
        row = connection.execute("SELECT room_code FROM participants WHERE token = ?", (body.token,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Saved attempt not found.")
    return _room_state(row["room_code"], body.token)


@app.post("/api/student/answer")
def submit_answer(body: AnswerBody) -> dict[str, Any]:
    with _database() as connection:
        student = connection.execute("SELECT * FROM participants WHERE token = ?", (body.token,)).fetchone()
        if student is None:
            raise HTTPException(status_code=404, detail="Participant not found.")
        room = connection.execute("SELECT * FROM rooms WHERE code = ?", (student["room_code"],)).fetchone()
        if room["status"] != "running" or room["deadline"] <= time.time() or student["status"] != "in_progress":
            raise HTTPException(status_code=409, detail="The quiz has ended.")
        quiz = json.loads(room["quiz_json"])
        question = next((item for item in quiz["questions"] if item["id"] == body.question_id), None)
        if question is None or body.question_id not in json.loads(student["question_order"]):
            raise HTTPException(status_code=400, detail="Unknown question.")
        option_order = json.loads(student["option_orders"])[body.question_id]
        if not 0 <= body.option_index < len(option_order):
            raise HTTPException(status_code=400, detail="Unknown answer option.")
        answers = json.loads(student["answers"])
        if body.question_id in answers:
            raise HTTPException(status_code=409, detail="An answer for this question has already been saved.")
        selected_index = option_order[body.option_index]
        correct = selected_index == question["correct_index"]
        elapsed = time.time() - room["started_at"]
        speed_ratio = max(0.0, min(1.0, 1 - elapsed / (quiz["duration_minutes"] * 60)))
        earned = round(1000 * (0.3 + 0.7 * speed_ratio)) if correct else 0
        answers[body.question_id] = {"option_index": selected_index, "correct": correct, "points": earned}
        next_correct_count = student["correct_count"] + int(correct)
        next_status = student["status"]
        next_finished = student["finished_at"]
        if len(answers) == len(quiz["questions"]):
            next_status = "completed"
            next_finished = time.time()
        connection.execute(
            "UPDATE participants SET answers = ?, score = score + ?, correct_count = ?, status = ?, finished_at = ?, last_seen = ? WHERE token = ?",
            (json.dumps(answers), earned, next_correct_count, next_status, next_finished, time.time(), body.token),
        )
    return {"correct": correct, "points": earned, "score": student["score"] + earned, "finished": next_status == "completed"}


@app.post("/api/student/warning")
def student_warning(body: TokenBody) -> dict[str, Any]:
    with _database() as connection:
        student = connection.execute("SELECT * FROM participants WHERE token = ?", (body.token,)).fetchone()
        if student is None:
            raise HTTPException(status_code=404, detail="Participant not found.")
        room = connection.execute("SELECT status FROM rooms WHERE code = ?", (student["room_code"],)).fetchone()
        if room["status"] != "running" or student["status"] != "in_progress":
            return {"warnings": student["warnings"], "blocked": student["status"] == "blocked"}
        warnings = student["warnings"] + 1
        blocked = warnings >= 2
        answers = json.loads(student["answers"])
        quiz = json.loads(connection.execute("SELECT quiz_json FROM rooms WHERE code = ?", (student["room_code"],)).fetchone()[0])
        finished_at = time.time() if blocked else None
        connection.execute(
            "UPDATE participants SET warnings = ?, status = ?, finished_at = ?, last_seen = ? WHERE token = ?",
            (warnings, "blocked" if blocked else "in_progress", finished_at, time.time(), body.token),
        )
    return {"warnings": warnings, "blocked": blocked, "answered": len(answers), "total": len(quiz["questions"])}


@app.post("/api/student/ping")
def student_ping(body: TokenBody) -> dict[str, Any]:
    with _database() as connection:
        cursor = connection.execute("UPDATE participants SET last_seen = ? WHERE token = ?", (time.time(), body.token))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Participant not found.")
    return {"ok": True}


@app.post("/api/teacher/rooms/{code}/start")
def start_room(code: str, request: Request) -> dict[str, Any]:
    _require_teacher(request)
    with _database() as connection:
        room = connection.execute("SELECT * FROM rooms WHERE code = ?", (code,)).fetchone()
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        count = connection.execute("SELECT COUNT(*) FROM participants WHERE room_code = ?", (code,)).fetchone()[0]
        if room["status"] != "waiting" or count == 0:
            raise HTTPException(status_code=409, detail="At least one student must join the lobby before the quiz starts.")
        started_at = time.time()
        quiz = json.loads(room["quiz_json"])
        deadline = started_at + quiz["duration_minutes"] * 60
        connection.execute("UPDATE rooms SET status = 'running', started_at = ?, deadline = ? WHERE code = ?", (started_at, deadline, code))
        connection.execute("UPDATE participants SET status = 'in_progress', last_seen = ? WHERE room_code = ?", (started_at, code))
    return _room_state(code)


@app.post("/api/teacher/rooms/{code}/finish")
def finish_room(code: str, request: Request) -> dict[str, Any]:
    _require_teacher(request)
    with _database() as connection:
        room = connection.execute("SELECT status FROM rooms WHERE code = ?", (code,)).fetchone()
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        connection.execute("UPDATE rooms SET status = 'finished' WHERE code = ?", (code,))
        connection.execute(
            "UPDATE participants SET status = CASE WHEN status = 'blocked' THEN status ELSE 'completed' END, "
            "finished_at = COALESCE(finished_at, ?) WHERE room_code = ?",
            (time.time(), code),
        )
    return _room_state(code)


@app.get("/api/teacher/rooms/{code}/export")
def export_results(code: str, request: Request) -> PlainTextResponse:
    _require_teacher(request)
    room = _room_row(code)
    quiz = json.loads(room["quiz_json"])
    board = _ranked_participants(code)
    lines = [f"Results: {quiz['title']}", f"Room code: {code}", "", "Place | Student | Points | Correct | Grade / 10 | Time (sec.)"]
    for item in board:
        elapsed = max(0, round((item["finished_at"] or time.time()) - item["joined_at"]))
        lines.append(
            f"{item['rank']} | {item['name']} | {item['score']} | {item['correct_count']}/{item['question_count']} | {item['grade']} | {elapsed}"
        )
    response = PlainTextResponse("\n".join(lines), media_type="text/plain; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="results-{code}.txt"'
    return response


@app.websocket("/ws")
async def websocket_updates(websocket: WebSocket) -> None:
    await websocket.accept()
    teacher = _valid_teacher_token(websocket.cookies.get(COOKIE_NAME))
    student_token = websocket.query_params.get("token")
    if not teacher and not student_token:
        await websocket.close(code=4401)
        return
    try:
        while True:
            try:
                if teacher:
                    with _database() as connection:
                        rows = connection.execute(
                            "SELECT code FROM rooms WHERE status IN ('waiting', 'running') ORDER BY created_at DESC"
                        ).fetchall()
                    payload = {"type": "teacher", "rooms": [_room_state(row["code"]) for row in rows]}
                else:
                    with _database() as connection:
                        participant = connection.execute(
                            "SELECT room_code FROM participants WHERE token = ?", (student_token,)
                        ).fetchone()
                    if participant is None:
                        await websocket.close(code=4404)
                        return
                    payload = {"type": "student", "room": _room_state(participant["room_code"], student_token)}
                await websocket.send_json(payload)
            except (HTTPException, sqlite3.Error, OSError):
                pass
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        return


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)