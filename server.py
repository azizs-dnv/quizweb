from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import random
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError


BASE_DIR = Path(__file__).resolve().parent
QUIZ_DIR = BASE_DIR / "tests" / "quizzes"
TEACHER_DATA_DIR = BASE_DIR / "teacher_data"
AUTH_FILE = BASE_DIR / "auth.json"
DB_FILE = BASE_DIR / "quizroom.db"
COOKIE_NAME = "quizroom_teacher"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
DEFAULT_TEACHER_ID = "default"
_REGISTRATION_LOCK = threading.Lock()
QUIZ_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="QuizRoom", version="1.0.0")


class LoginBody(BaseModel):
    username: str
    password: str


class RegisterBody(BaseModel):
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
    answer: Any = None
    option_index: int | None = None


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
                owner_id TEXT NOT NULL DEFAULT 'default',
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
                question_times TEXT NOT NULL DEFAULT '{}',
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
        room_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(rooms)").fetchall()
        }
        if "owner_id" not in room_columns:
            connection.execute(
                "ALTER TABLE rooms ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'default'"
            )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(participants)").fetchall()
        }
        if "question_times" not in columns:
            connection.execute(
                "ALTER TABLE participants ADD COLUMN question_times TEXT NOT NULL DEFAULT '{}'"
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


def _valid_teacher_id(teacher_id: str) -> bool:
    return len(teacher_id) == 24 and all(character in "0123456789abcdef" for character in teacher_id)


def _account_for_id(teacher_id: str) -> dict[str, str] | None:
    if teacher_id == DEFAULT_TEACHER_ID:
        return _read_auth()
    if not _valid_teacher_id(teacher_id):
        return None
    account_path = TEACHER_DATA_DIR / teacher_id / "account.json"
    try:
        account = json.loads(account_path.read_text(encoding="utf-8"))
        if all(key in account for key in ("username", "password_salt", "password_hash")):
            return account
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _account_for_username(username: str) -> tuple[str, dict[str, str]] | None:
    normalized = username.strip().casefold()
    default_account = _read_auth()
    if default_account and default_account["username"].casefold() == normalized:
        return DEFAULT_TEACHER_ID, default_account
    if TEACHER_DATA_DIR.exists():
        for account_path in TEACHER_DATA_DIR.glob("*/account.json"):
            teacher_id = account_path.parent.name
            account = _account_for_id(teacher_id)
            if account and account["username"].casefold() == normalized:
                return teacher_id, account
    return None


def _quiz_directory(teacher_id: str) -> Path:
    if teacher_id == DEFAULT_TEACHER_ID:
        return QUIZ_DIR
    if not _valid_teacher_id(teacher_id) or _account_for_id(teacher_id) is None:
        raise HTTPException(status_code=401, detail="Teacher account not found.")
    directory = TEACHER_DATA_DIR / teacher_id / "quizzes"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _sign_session(secret: str, teacher_id: str = DEFAULT_TEACHER_ID, issued_at: int | None = None) -> str:
    payload = f"teacher:{teacher_id}:{issued_at or int(time.time())}"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _valid_teacher_token(token: str | None) -> str | None:
    auth = _read_auth()
    if not auth or not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    expected = hmac.new(auth["token_secret"].encode(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        parts = payload.split(":")
        if len(parts) == 2:
            kind, issued_at = parts
            teacher_id = DEFAULT_TEACHER_ID
        elif len(parts) == 3:
            kind, teacher_id, issued_at = parts
        else:
            return None
        age = int(time.time()) - int(issued_at)
        if kind == "teacher" and 0 <= age <= SESSION_MAX_AGE and _account_for_id(teacher_id):
            return teacher_id
        return None
    except (ValueError, UnicodeDecodeError):
        return None


def _require_teacher(request: Request) -> str:
    teacher_id = _valid_teacher_token(request.cookies.get(COOKIE_NAME))
    if teacher_id is None:
        raise HTTPException(status_code=401, detail="Teacher sign-in is required.")
    return teacher_id


def _quiz_path(quiz_id: str, teacher_id: str = DEFAULT_TEACHER_ID) -> Path:
    if not quiz_id.isalnum():
        raise HTTPException(status_code=404, detail="Quiz not found.")
    return _quiz_directory(teacher_id) / f"{quiz_id}.json"


def _load_quiz(quiz_id: str, teacher_id: str = DEFAULT_TEACHER_ID) -> dict[str, Any]:
    path = _quiz_path(quiz_id, teacher_id)
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


QUESTION_TYPES = {"single_choice", "multiple_choice", "text", "matching"}


def _question_type(question: dict[str, Any]) -> str:
    return question.get("type", "single_choice")


def _normalise_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _points_for_correct_answer(elapsed_seconds: float, expected_seconds: float) -> int:
    speed_ratio = max(0.0, min(1.0, 1 - elapsed_seconds / max(1.0, expected_seconds)))
    return round(300 + 700 * speed_ratio)


def _evaluate_answer(
    question: dict[str, Any], submitted: Any, option_order: list[int] | dict[str, list[int]]
) -> tuple[bool, Any]:
    question_type = _question_type(question)
    if question_type == "single_choice":
        if type(submitted) is not int or not 0 <= submitted < len(option_order):
            raise HTTPException(status_code=400, detail="Choose one valid answer option.")
        selected = option_order[submitted]
        return selected == question["correct_index"], selected

    if question_type == "multiple_choice":
        if (
            not isinstance(submitted, list)
            or not submitted
            or any(type(index) is not int or not 0 <= index < len(option_order) for index in submitted)
            or len(set(submitted)) != len(submitted)
        ):
            raise HTTPException(status_code=400, detail="Choose one or more valid answer options.")
        selected = sorted(option_order[index] for index in submitted)
        return set(selected) == set(question["correct_indices"]), selected

    if question_type == "text":
        if not isinstance(submitted, str) or not submitted.strip() or len(submitted) > 1000:
            raise HTTPException(status_code=400, detail="Enter a text answer up to 1,000 characters.")
        answer = submitted.strip()
        return _normalise_text(answer) == _normalise_text(question["expected_answer"]), answer

    if question_type == "matching":
        pairs = question["pairs"]
        right_order = option_order["right"]
        expected_keys = {str(index) for index in range(len(pairs))}
        if (
            not isinstance(submitted, dict)
            or set(submitted) != expected_keys
            or any(type(index) is not int or not 0 <= index < len(right_order) for index in submitted.values())
            or len(set(submitted.values())) != len(pairs)
        ):
            raise HTTPException(status_code=400, detail="Match every item with a unique answer.")
        selected = {
            int(left_index): right_order[display_index]
            for left_index, display_index in submitted.items()
        }
        return all(left_index == right_index for left_index, right_index in selected.items()), selected

    raise HTTPException(status_code=400, detail="Unsupported question type.")


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
        answers = json.loads(student["answers"])
        question_times = json.loads(student["question_times"])
        if room["status"] == "running" and student["status"] == "in_progress":
            active_question = next(
                (question_id for question_id in json.loads(student["question_order"]) if question_id not in answers),
                None,
            )
            if active_question and active_question not in question_times:
                question_times[active_question] = time.time()
                connection.execute(
                    "UPDATE participants SET question_times = ? WHERE token = ?",
                    (json.dumps(question_times), token),
                )
        ranked_tokens = connection.execute(
            "SELECT token FROM participants WHERE room_code = ? ORDER BY score DESC, correct_count DESC, joined_at ASC",
            (code,),
        ).fetchall()

    order = json.loads(student["question_order"])
    option_orders = json.loads(student["option_orders"])
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
            question_type = _question_type(question)
            item = {
                "id": question_id,
                "type": question_type,
                "text": question["text"],
                "answered": question_id in answers,
            }
            if question_type in {"single_choice", "multiple_choice"}:
                item["options"] = [question["options"][index] for index in option_orders[question_id]]
            elif question_type == "matching":
                orders = option_orders[question_id]
                item["pairs"] = [
                    {"key": index, "left": question["pairs"][index]["left"]}
                    for index in orders["left"]
                ]
                item["match_options"] = [question["pairs"][index]["right"] for index in orders["right"]]
            ordered_questions.append(item)
        result["questions"] = ordered_questions
    return result


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
    root_auth = _read_auth()
    if not root_auth:
        raise HTTPException(status_code=409, detail="Run init_admin.py to create the teacher account first.")
    account_entry = _account_for_username(body.username)
    if account_entry is None:
        raise HTTPException(status_code=401, detail="Incorrect username or password.")
    teacher_id, auth = account_entry
    candidate = hashlib.scrypt(
        body.password.encode(), salt=bytes.fromhex(auth["password_salt"]), n=2**14, r=8, p=1, dklen=32
    ).hex()
    if not hmac.compare_digest(candidate, auth["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect username or password.")
    token = _sign_session(root_auth["token_secret"], teacher_id)
    result = JSONResponse({"username": auth["username"], "teacher_id": teacher_id, "token": token})
    result.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=SESSION_MAX_AGE)
    return result


@app.post("/api/auth/register", status_code=201)
def register_teacher(body: RegisterBody):
    root_auth = _read_auth()
    if not root_auth:
        raise HTTPException(status_code=409, detail="Run init_admin.py to create the first teacher account.")
    username = body.username.strip()
    if not 3 <= len(username) <= 80:
        raise HTTPException(status_code=422, detail="Username must contain between 3 and 80 characters.")
    if len(body.password) < 10:
        raise HTTPException(status_code=422, detail="Password must contain at least 10 characters.")

    with _REGISTRATION_LOCK:
        if _account_for_username(username):
            raise HTTPException(status_code=409, detail="That username is already in use.")
        teacher_id = secrets.token_hex(12)
        salt = secrets.token_bytes(16)
        account = {
            "username": username,
            "password_salt": salt.hex(),
            "password_hash": hashlib.scrypt(
                body.password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32
            ).hex(),
            "created_at": time.time(),
        }
        account_directory = TEACHER_DATA_DIR / teacher_id
        account_directory.mkdir(parents=True, exist_ok=False)
        account_file = account_directory / "account.json"
        temporary_file = account_directory / "account.tmp"
        temporary_file.write_text(json.dumps(account, indent=2), encoding="utf-8")
        try:
            temporary_file.chmod(0o600)
        except OSError:
            pass
        temporary_file.replace(account_file)

    token = _sign_session(root_auth["token_secret"], teacher_id)
    result = JSONResponse(
        {"username": username, "teacher_id": teacher_id, "token": token}, status_code=201
    )
    result.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=SESSION_MAX_AGE)
    return result


@app.post("/api/auth/logout")
def logout():
    result = JSONResponse({"ok": True})
    result.delete_cookie(COOKIE_NAME)
    return result


def _build_quiz(title: str, duration_minutes: int, raw_questions: list[dict[str, Any]]) -> dict[str, Any]:
    title = title.strip()
    if not title or len(title) > 120:
        raise HTTPException(status_code=422, detail="Quiz title must be between 1 and 120 characters.")
    if not 1 <= duration_minutes <= 240:
        raise HTTPException(status_code=422, detail="Duration must be between 1 and 240 minutes.")
    if not 1 <= len(raw_questions) <= 100:
        raise HTTPException(status_code=422, detail="Add between 1 and 100 questions.")
    questions = []
    for position, item in enumerate(raw_questions, start=1):
        text = str(item.get("text", "")).strip()
        question_type = item.get("type", "single_choice")
        if question_type not in QUESTION_TYPES:
            raise HTTPException(status_code=422, detail=f"Unsupported question type for question {position}.")
        if not text or len(text) > 1000:
            raise HTTPException(status_code=422, detail=f"Enter a question up to 1,000 characters for question {position}.")
        question = {"id": secrets.token_hex(6), "type": question_type, "text": text}
        if question_type in {"single_choice", "multiple_choice"}:
            raw_options = item.get("options", [])
            if not isinstance(raw_options, list):
                raise HTTPException(status_code=422, detail=f"Answer options must be a list for question {position}.")
            options = [str(option).strip() for option in raw_options]
            if not 2 <= len(options) <= 12 or any(not option for option in options):
                raise HTTPException(status_code=422, detail=f"Add 2 to 12 non-empty options for question {position}.")
            question["options"] = options
            if question_type == "single_choice":
                correct_index = item.get("correct_index")
                if type(correct_index) is not int or not 0 <= correct_index < len(options):
                    raise HTTPException(status_code=422, detail=f"Select one correct answer for question {position}.")
                question["correct_index"] = correct_index
            else:
                correct_indices = item.get("correct_indices")
                if (
                    not isinstance(correct_indices, list)
                    or not correct_indices
                    or any(type(index) is not int or not 0 <= index < len(options) for index in correct_indices)
                    or len(set(correct_indices)) != len(correct_indices)
                ):
                    raise HTTPException(status_code=422, detail=f"Select one or more correct options for question {position}.")
                question["correct_indices"] = correct_indices
        elif question_type == "text":
            expected_answer = str(item.get("expected_answer", "")).strip()
            if not expected_answer or len(expected_answer) > 1000:
                raise HTTPException(status_code=422, detail=f"Enter the expected text answer for question {position}.")
            question["expected_answer"] = expected_answer
        else:
            raw_pairs = item.get("pairs", [])
            if not isinstance(raw_pairs, list) or not 2 <= len(raw_pairs) <= 12:
                raise HTTPException(status_code=422, detail=f"Add 2 to 12 matching pairs for question {position}.")
            pairs = [
                {"left": str(pair.get("left", "")).strip(), "right": str(pair.get("right", "")).strip()}
                for pair in raw_pairs
                if isinstance(pair, dict)
            ]
            if len(pairs) != len(raw_pairs) or any(not pair["left"] or not pair["right"] for pair in pairs):
                raise HTTPException(status_code=422, detail=f"Fill in both sides of every pair for question {position}.")
            if (
                len({_normalise_text(pair["left"]) for pair in pairs}) != len(pairs)
                or len({_normalise_text(pair["right"]) for pair in pairs}) != len(pairs)
            ):
                raise HTTPException(status_code=422, detail=f"Matching items must be unique on each side for question {position}.")
            question["pairs"] = pairs
        questions.append(question)
    return {
        "id": secrets.token_hex(8),
        "title": title,
        "duration_minutes": duration_minutes,
        "questions": questions,
        "created_at": time.time(),
    }


def _quiz_has_active_room(quiz_id: str, teacher_id: str) -> bool:
    with _database() as connection:
        return connection.execute(
            "SELECT 1 FROM rooms WHERE quiz_id = ? AND owner_id = ? AND status IN ('waiting', 'running') LIMIT 1",
            (quiz_id, teacher_id),
        ).fetchone() is not None


def _write_quiz(quiz: dict[str, Any], teacher_id: str) -> None:
    path = _quiz_path(quiz["id"], teacher_id)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(quiz, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@app.get("/api/teacher/quizzes")
def list_quizzes(request: Request) -> list[dict[str, Any]]:
    teacher_id = _require_teacher(request)
    directory = _quiz_directory(teacher_id)
    quizzes = []
    for path in sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            quiz = json.loads(path.read_text(encoding="utf-8"))
            summary = _public_quiz(quiz)
            summary["active"] = _quiz_has_active_room(quiz["id"], teacher_id)
            quizzes.append(summary)
        except (OSError, json.JSONDecodeError):
            continue
    return quizzes


@app.post("/api/teacher/quizzes", status_code=201)
def create_quiz(body: QuizBody, request: Request) -> dict[str, Any]:
    teacher_id = _require_teacher(request)
    quiz = _build_quiz(body.title, body.duration_minutes, body.questions)
    _write_quiz(quiz, teacher_id)
    return _public_quiz(quiz)


@app.get("/api/teacher/quizzes/{quiz_id}")
def get_quiz(quiz_id: str, request: Request) -> dict[str, Any]:
    teacher_id = _require_teacher(request)
    return _load_quiz(quiz_id, teacher_id)


@app.put("/api/teacher/quizzes/{quiz_id}")
def update_quiz(quiz_id: str, body: QuizBody, request: Request) -> dict[str, Any]:
    teacher_id = _require_teacher(request)
    path = _quiz_path(quiz_id, teacher_id)
    existing = _load_quiz(quiz_id, teacher_id)
    if _quiz_has_active_room(quiz_id, teacher_id):
        raise HTTPException(status_code=409, detail="Finish this quiz's active room before editing it.")
    quiz = _build_quiz(body.title, body.duration_minutes, body.questions)
    quiz["id"] = quiz_id
    quiz["created_at"] = existing.get("created_at", time.time())
    _write_quiz(quiz, teacher_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Quiz not found.")
    return _public_quiz(quiz)


@app.post("/api/teacher/quizzes/import", status_code=201)
async def import_quizzes(request: Request) -> dict[str, Any]:
    teacher_id = _require_teacher(request)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail="Import file must contain valid JSON.") from None
    if isinstance(payload, dict) and "quizzes" in payload:
        raw_quizzes = payload["quizzes"]
    elif isinstance(payload, list):
        raw_quizzes = payload
    else:
        raw_quizzes = [payload]
    if not isinstance(raw_quizzes, list) or not 1 <= len(raw_quizzes) <= 50:
        raise HTTPException(status_code=422, detail="Import 1 to 50 quizzes at a time.")

    imported = []
    for raw_quiz in raw_quizzes:
        if not isinstance(raw_quiz, dict):
            raise HTTPException(status_code=422, detail="Each imported quiz must be a JSON object.")
        try:
            body = QuizBody(**raw_quiz)
        except ValidationError:
            raise HTTPException(status_code=422, detail="Imported quiz has an invalid structure.") from None
        imported.append(_build_quiz(body.title, body.duration_minutes, body.questions))

    for quiz in imported:
        _write_quiz(quiz, teacher_id)
    return {"imported_count": len(imported), "quizzes": [_public_quiz(quiz) for quiz in imported]}


@app.delete("/api/teacher/quizzes/{quiz_id}")
def delete_quiz(quiz_id: str, request: Request) -> dict[str, bool]:
    teacher_id = _require_teacher(request)
    path = _quiz_path(quiz_id, teacher_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Quiz not found.")
    if _quiz_has_active_room(quiz_id, teacher_id):
        raise HTTPException(status_code=409, detail="Finish the active room for this quiz before deleting it.")
    path.unlink()
    return {"ok": True}


@app.get("/api/teacher/rooms")
def list_rooms(request: Request) -> list[dict[str, Any]]:
    teacher_id = _require_teacher(request)
    with _database() as connection:
        rows = connection.execute(
            "SELECT code FROM rooms WHERE owner_id = ? ORDER BY created_at DESC LIMIT 30", (teacher_id,)
        ).fetchall()
    return [_room_state(row["code"]) for row in rows]


@app.post("/api/teacher/rooms/{quiz_id}", status_code=201)
def create_room(quiz_id: str, request: Request) -> dict[str, Any]:
    teacher_id = _require_teacher(request)
    quiz = _load_quiz(quiz_id, teacher_id)
    with _database() as connection:
        for _ in range(20):
            code = f"{secrets.randbelow(900000) + 100000}"
            try:
                connection.execute(
                    "INSERT INTO rooms(code, quiz_id, owner_id, quiz_json, status, created_at) "
                    "VALUES (?, ?, ?, ?, 'waiting', ?)",
                    (code, quiz_id, teacher_id, json.dumps(quiz, ensure_ascii=False), time.time()),
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
        question_type = _question_type(question)
        if question_type == "matching":
            left_order = list(range(len(question["pairs"])))
            right_order = left_order.copy()
            random.SystemRandom().shuffle(left_order)
            random.SystemRandom().shuffle(right_order)
            option_orders[question["id"]] = {"left": left_order, "right": right_order}
        elif question_type == "text":
            option_orders[question["id"]] = []
        else:
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
        answers = json.loads(student["answers"])
        if body.question_id in answers:
            raise HTTPException(status_code=409, detail="An answer for this question has already been saved.")
        active_question = next(
            (question_id for question_id in json.loads(student["question_order"]) if question_id not in answers),
            None,
        )
        if body.question_id != active_question:
            raise HTTPException(status_code=409, detail="Answer the active question first.")
        submitted = body.answer if body.answer is not None else body.option_index
        if submitted is None:
            raise HTTPException(status_code=400, detail="An answer is required.")
        option_order = json.loads(student["option_orders"])[body.question_id]
        correct, saved_answer = _evaluate_answer(question, submitted, option_order)
        now = time.time()
        question_times = json.loads(student["question_times"])
        answered_at = question_times.get(body.question_id, now)
        expected_seconds = quiz["duration_minutes"] * 60 / len(quiz["questions"])
        earned = _points_for_correct_answer(now - answered_at, expected_seconds) if correct else 0
        answers[body.question_id] = {"answer": saved_answer, "correct": correct, "points": earned}
        question_times.pop(body.question_id, None)
        next_correct_count = student["correct_count"] + int(correct)
        next_status = student["status"]
        next_finished = student["finished_at"]
        if len(answers) == len(quiz["questions"]):
            next_status = "completed"
            next_finished = time.time()
        connection.execute(
            "UPDATE participants SET answers = ?, question_times = ?, score = score + ?, correct_count = ?, status = ?, finished_at = ?, last_seen = ? WHERE token = ?",
            (json.dumps(answers), json.dumps(question_times), earned, next_correct_count, next_status, next_finished, now, body.token),
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
    teacher_id = _require_teacher(request)
    with _database() as connection:
        room = connection.execute(
            "SELECT * FROM rooms WHERE code = ? AND owner_id = ?", (code, teacher_id)
        ).fetchone()
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
    teacher_id = _require_teacher(request)
    with _database() as connection:
        room = connection.execute(
            "SELECT status FROM rooms WHERE code = ? AND owner_id = ?", (code, teacher_id)
        ).fetchone()
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        connection.execute(
            "UPDATE rooms SET status = 'finished' WHERE code = ? AND owner_id = ?", (code, teacher_id)
        )
        connection.execute(
            "UPDATE participants SET status = CASE WHEN status = 'blocked' THEN status ELSE 'completed' END, "
            "finished_at = COALESCE(finished_at, ?) WHERE room_code = ?",
            (time.time(), code),
        )
    return _room_state(code)


@app.get("/api/teacher/rooms/{code}/export")
def export_results(code: str, request: Request) -> PlainTextResponse:
    teacher_id = _require_teacher(request)
    room = _room_row(code)
    if room["owner_id"] != teacher_id:
        raise HTTPException(status_code=404, detail="Room not found.")
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
    teacher_id = _valid_teacher_token(websocket.cookies.get(COOKIE_NAME))
    student_token = websocket.query_params.get("token")
    if teacher_id is None and not student_token:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    try:
        while True:
            try:
                if teacher_id is not None:
                    with _database() as connection:
                        rows = connection.execute(
                            "SELECT code FROM rooms WHERE owner_id = ? AND status IN ('waiting', 'running') "
                            "ORDER BY created_at DESC",
                            (teacher_id,),
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