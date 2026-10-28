from __future__ import annotations

from fastapi import APIRouter

import sys

core = sys.modules.get("server") or sys.modules.get("__main__")
if core is None:
    raise RuntimeError("The QuizRoom application module is not loaded.")

router = APIRouter()


@router.get("/api/teacher/rooms")
def list_rooms(request: core.Request) -> list[dict[str, core.Any]]:
    teacher_id = core._require_teacher(request)
    with core._database() as connection:
        rows = connection.execute(
            "SELECT code FROM rooms WHERE owner_id = ? ORDER BY created_at DESC LIMIT 30",
            (teacher_id,),
        ).fetchall()
    return [core._room_state(row["code"]) for row in rows]


@router.post("/api/teacher/rooms/{quiz_id}", status_code=201)
def create_room(quiz_id: str, request: core.Request) -> dict[str, core.Any]:
    teacher_id = core._require_teacher(request)
    quiz = core._load_quiz(quiz_id, teacher_id)
    with core._database() as connection:
        for _ in range(20):
            code = f"{core.secrets.randbelow(900000) + 100000}"
            try:
                connection.execute(
                    "INSERT INTO rooms(code, quiz_id, owner_id, quiz_json, status, created_at) VALUES (?, ?, ?, ?, 'waiting', ?)",
                    (
                        code,
                        quiz_id,
                        teacher_id,
                        core.json.dumps(quiz, ensure_ascii=False),
                        core.time.time(),
                    ),
                )
                break
            except core.sqlite3.IntegrityError:
                continue
        else:
            raise core.HTTPException(
                status_code=503, detail="Could not generate a room code."
            )
    return core._room_state(code)


@router.post("/api/student/join", status_code=201)
def join_room(body: core.JoinBody) -> dict[str, core.Any]:
    code = body.code.strip()
    name = " ".join(body.name.split())
    if not name or len(name) > 60:
        raise core.HTTPException(
            status_code=422, detail="Enter a name (up to 60 characters)."
        )
    core._refresh_room()
    room = core._room_row(code)
    if room["status"] not in {"waiting", "running"}:
        raise core.HTTPException(
            status_code=409, detail="This room is no longer accepting participants."
        )
    quiz = core.json.loads(room["quiz_json"])
    questions = quiz["questions"]
    question_order = [question["id"] for question in questions]
    core.random.SystemRandom().shuffle(question_order)
    option_orders = {}
    for question in questions:
        question_type = core._question_type(question)
        if question_type == "matching":
            left_order = list(range(len(question["pairs"])))
            right_order = left_order.copy()
            core.random.SystemRandom().shuffle(left_order)
            core.random.SystemRandom().shuffle(right_order)
            option_orders[question["id"]] = {"left": left_order, "right": right_order}
        elif question_type == "text":
            option_orders[question["id"]] = []
        else:
            indices = list(range(len(question["options"])))
            core.random.SystemRandom().shuffle(indices)
            option_orders[question["id"]] = indices
    token = core.secrets.token_urlsafe(32)
    now = core.time.time()
    participant_status = "in_progress" if room["status"] == "running" else "waiting"
    with core._database() as connection:
        connection.execute(
            "INSERT INTO participants(token, room_code, name, question_order, option_orders, status, joined_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token,
                code,
                name,
                core.json.dumps(question_order),
                core.json.dumps(option_orders),
                participant_status,
                now,
                now,
            ),
        )
    return core._room_state(code, token)


@router.post("/api/student/resume")
def resume_student(body: core.TokenBody) -> dict[str, core.Any]:
    with core._database() as connection:
        row = connection.execute(
            "SELECT room_code FROM participants WHERE token = ?", (body.token,)
        ).fetchone()
    if row is None:
        raise core.HTTPException(status_code=404, detail="Saved attempt not found.")
    return core._room_state(row["room_code"], body.token)


@router.post("/api/student/answer")
def submit_answer(body: core.AnswerBody) -> dict[str, core.Any]:
    with core._database() as connection:
        student = connection.execute(
            "SELECT * FROM participants WHERE token = ?", (body.token,)
        ).fetchone()
        if student is None:
            raise core.HTTPException(status_code=404, detail="Participant not found.")
        room = connection.execute(
            "SELECT * FROM rooms WHERE code = ?", (student["room_code"],)
        ).fetchone()
        if (
            room["status"] != "running"
            or room["deadline"] <= core.time.time()
            or student["status"] != "in_progress"
        ):
            raise core.HTTPException(status_code=409, detail="The quiz has ended.")
        quiz = core.json.loads(room["quiz_json"])
        question = next(
            (item for item in quiz["questions"] if item["id"] == body.question_id), None
        )
        if question is None or body.question_id not in core.json.loads(
            student["question_order"]
        ):
            raise core.HTTPException(status_code=400, detail="Unknown question.")
        answers = core.json.loads(student["answers"])
        if body.question_id in answers:
            raise core.HTTPException(
                status_code=409,
                detail="An answer for this question has already been saved.",
            )
        active_question = next(
            (
                question_id
                for question_id in core.json.loads(student["question_order"])
                if question_id not in answers
            ),
            None,
        )
        if body.question_id != active_question:
            raise core.HTTPException(
                status_code=409, detail="Answer the active question first."
            )
        submitted = body.answer if body.answer is not None else body.option_index
        if submitted is None:
            raise core.HTTPException(status_code=400, detail="An answer is required.")
        option_order = core.json.loads(student["option_orders"])[body.question_id]
        correct, saved_answer = core._evaluate_answer(question, submitted, option_order)
        now = core.time.time()
        question_times = core.json.loads(student["question_times"])
        answered_at = question_times.get(body.question_id, now)
        total_seconds = quiz["duration_minutes"] * 60
        expected_seconds = total_seconds / len(quiz["questions"])
        late_seconds = max(0.0, student["joined_at"] - room["started_at"])
        remaining_time_ratio = max(0.0, 1 - late_seconds / total_seconds)
        earned = (
            round(
                core._points_for_correct_answer(now - answered_at, expected_seconds)
                * remaining_time_ratio
            )
            if correct
            else 0
        )
        answers[body.question_id] = {
            "answer": saved_answer,
            "correct": correct,
            "points": earned,
        }
        question_times.pop(body.question_id, None)
        next_correct_count = student["correct_count"] + int(correct)
        next_status = student["status"]
        next_finished = student["finished_at"]
        if len(answers) == len(quiz["questions"]):
            next_status = "completed"
            next_finished = core.time.time()
        connection.execute(
            "UPDATE participants SET answers = ?, question_times = ?, score = score + ?, correct_count = ?, status = ?, finished_at = ?, last_seen = ? WHERE token = ?",
            (
                core.json.dumps(answers),
                core.json.dumps(question_times),
                earned,
                next_correct_count,
                next_status,
                next_finished,
                now,
                body.token,
            ),
        )
    return {
        "correct": correct,
        "points": earned,
        "score": student["score"] + earned,
        "finished": next_status == "completed",
    }


@router.post("/api/student/warning")
def student_warning(body: core.TokenBody) -> dict[str, core.Any]:
    with core._database() as connection:
        student = connection.execute(
            "SELECT * FROM participants WHERE token = ?", (body.token,)
        ).fetchone()
        if student is None:
            raise core.HTTPException(status_code=404, detail="Participant not found.")
        room = connection.execute(
            "SELECT status FROM rooms WHERE code = ?", (student["room_code"],)
        ).fetchone()
        if room["status"] != "running" or student["status"] != "in_progress":
            return {
                "warnings": student["warnings"],
                "blocked": student["status"] == "blocked",
            }
        warnings = student["warnings"] + 1
        blocked = warnings >= 2
        answers = core.json.loads(student["answers"])
        quiz = core.json.loads(
            connection.execute(
                "SELECT quiz_json FROM rooms WHERE code = ?", (student["room_code"],)
            ).fetchone()[0]
        )
        finished_at = core.time.time() if blocked else None
        connection.execute(
            "UPDATE participants SET warnings = ?, status = ?, finished_at = ?, last_seen = ? WHERE token = ?",
            (
                warnings,
                "blocked" if blocked else "in_progress",
                finished_at,
                core.time.time(),
                body.token,
            ),
        )
    return {
        "warnings": warnings,
        "blocked": blocked,
        "answered": len(answers),
        "total": len(quiz["questions"]),
    }


@router.post("/api/student/ping")
def student_ping(body: core.TokenBody) -> dict[str, core.Any]:
    with core._database() as connection:
        cursor = connection.execute(
            "UPDATE participants SET last_seen = ? WHERE token = ?",
            (core.time.time(), body.token),
        )
        if cursor.rowcount == 0:
            raise core.HTTPException(status_code=404, detail="Participant not found.")
    return {"ok": True}


@router.post("/api/teacher/rooms/{code}/start")
def start_room(code: str, request: core.Request) -> dict[str, core.Any]:
    teacher_id = core._require_teacher(request)
    with core._database() as connection:
        room = connection.execute(
            "SELECT * FROM rooms WHERE code = ? AND owner_id = ?", (code, teacher_id)
        ).fetchone()
        if room is None:
            raise core.HTTPException(status_code=404, detail="Room not found.")
        count = connection.execute(
            "SELECT COUNT(*) FROM participants WHERE room_code = ?", (code,)
        ).fetchone()[0]
        if room["status"] != "waiting" or count == 0:
            raise core.HTTPException(
                status_code=409,
                detail="At least one student must join the lobby before the quiz starts.",
            )
        started_at = core.time.time()
        quiz = core.json.loads(room["quiz_json"])
        deadline = started_at + quiz["duration_minutes"] * 60
        connection.execute(
            "UPDATE rooms SET status = 'running', started_at = ?, deadline = ? WHERE code = ?",
            (started_at, deadline, code),
        )
        connection.execute(
            "UPDATE participants SET status = 'in_progress', last_seen = ? WHERE room_code = ?",
            (started_at, code),
        )
    return core._room_state(code)


@router.post("/api/teacher/rooms/{code}/finish")
def finish_room(code: str, request: core.Request) -> dict[str, core.Any]:
    teacher_id = core._require_teacher(request)
    with core._database() as connection:
        room = connection.execute(
            "SELECT status FROM rooms WHERE code = ? AND owner_id = ?",
            (code, teacher_id),
        ).fetchone()
        if room is None:
            raise core.HTTPException(status_code=404, detail="Room not found.")
        connection.execute(
            "UPDATE rooms SET status = 'finished' WHERE code = ? AND owner_id = ?",
            (code, teacher_id),
        )
        connection.execute(
            "UPDATE participants SET status = CASE WHEN status = 'blocked' THEN status ELSE 'completed' END, finished_at = COALESCE(finished_at, ?) WHERE room_code = ?",
            (core.time.time(), code),
        )
    return core._room_state(code)


@router.get("/api/teacher/rooms/{code}/export")
def export_results(code: str, request: core.Request) -> core.PlainTextResponse:
    teacher_id = core._require_teacher(request)
    room = core._room_row(code)
    if room["owner_id"] != teacher_id:
        raise core.HTTPException(status_code=404, detail="Room not found.")
    quiz = core.json.loads(room["quiz_json"])
    board = core._ranked_participants(code)
    lines = [
        f"Results: {quiz['title']}",
        f"Room code: {code}",
        "",
        "Place | Student | Points | Correct | Grade / 10 | Time (sec.)",
    ]
    for item in board:
        elapsed = max(
            0, round((item["finished_at"] or core.time.time()) - item["joined_at"])
        )
        lines.append(
            f"{item['rank']} | {item['name']} | {item['score']} | {item['correct_count']}/{item['question_count']} | {item['grade']} | {elapsed}"
        )
    response = core.PlainTextResponse(
        "\n".join(lines), media_type="text/plain; charset=utf-8"
    )
    response.headers["Content-Disposition"] = (
        f'attachment; filename="results-{code}.txt"'
    )
    return response


@router.websocket("/ws")
async def websocket_updates(websocket: core.WebSocket) -> None:
    teacher_id = core._valid_teacher_token(websocket.cookies.get(core.COOKIE_NAME))
    student_token = websocket.query_params.get("token")
    if teacher_id is None and (not student_token):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    try:
        while True:
            try:
                if teacher_id is not None:
                    with core._database() as connection:
                        rows = connection.execute(
                            "SELECT code FROM rooms WHERE owner_id = ? AND status IN ('waiting', 'running') ORDER BY created_at DESC",
                            (teacher_id,),
                        ).fetchall()
                    payload = {
                        "type": "teacher",
                        "rooms": [core._room_state(row["code"]) for row in rows],
                    }
                else:
                    with core._database() as connection:
                        participant = connection.execute(
                            "SELECT room_code FROM participants WHERE token = ?",
                            (student_token,),
                        ).fetchone()
                    if participant is None:
                        await websocket.close(code=4404)
                        return
                    payload = {
                        "type": "student",
                        "room": core._room_state(
                            participant["room_code"], student_token
                        ),
                    }
                await websocket.send_json(payload)
            except (core.HTTPException, core.sqlite3.Error, OSError):
                pass
            await core.asyncio.sleep(1)
    except core.WebSocketDisconnect:
        return
