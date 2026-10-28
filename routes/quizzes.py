from __future__ import annotations

from fastapi import APIRouter

import sys

core = sys.modules.get("server") or sys.modules.get("__main__")
if core is None:
    raise RuntimeError("The QuizRoom application module is not loaded.")

router = APIRouter()


@router.get("/api/teacher/quizzes")
def list_quizzes(request: core.Request) -> list[dict[str, core.Any]]:
    teacher_id = core._require_teacher(request)
    directory = core._quiz_directory(teacher_id)
    quizzes = []
    for path in sorted(
        directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True
    ):
        try:
            quiz = core.json.loads(path.read_text(encoding="utf-8"))
            summary = core._public_quiz(quiz)
            summary["active"] = core._quiz_has_active_room(quiz["id"], teacher_id)
            quizzes.append(summary)
        except (OSError, core.json.JSONDecodeError):
            continue
    return quizzes


@router.post("/api/teacher/quizzes", status_code=201)
def create_quiz(body: core.QuizBody, request: core.Request) -> dict[str, core.Any]:
    teacher_id = core._require_teacher(request)
    quiz = core._build_quiz(body.title, body.duration_minutes, body.questions)
    core._write_quiz(quiz, teacher_id)
    return core._public_quiz(quiz)


@router.get("/api/teacher/quizzes/{quiz_id}")
def get_quiz(quiz_id: str, request: core.Request) -> dict[str, core.Any]:
    teacher_id = core._require_teacher(request)
    return core._load_quiz(quiz_id, teacher_id)


@router.put("/api/teacher/quizzes/{quiz_id}")
def update_quiz(
    quiz_id: str, body: core.QuizBody, request: core.Request
) -> dict[str, core.Any]:
    teacher_id = core._require_teacher(request)
    path = core._quiz_path(quiz_id, teacher_id)
    existing = core._load_quiz(quiz_id, teacher_id)
    if core._quiz_has_active_room(quiz_id, teacher_id):
        raise core.HTTPException(
            status_code=409, detail="Finish this quiz's active room before editing it."
        )
    quiz = core._build_quiz(body.title, body.duration_minutes, body.questions)
    quiz["id"] = quiz_id
    quiz["created_at"] = existing.get("created_at", core.time.time())
    core._write_quiz(quiz, teacher_id)
    if not path.exists():
        raise core.HTTPException(status_code=404, detail="Quiz not found.")
    return core._public_quiz(quiz)


@router.post("/api/teacher/quizzes/import", status_code=201)
async def import_quizzes(request: core.Request) -> dict[str, core.Any]:
    teacher_id = core._require_teacher(request)
    try:
        payload = await request.json()
    except ValueError:
        raise core.HTTPException(
            status_code=422, detail="Import file must contain valid JSON."
        ) from None
    if isinstance(payload, dict) and "quizzes" in payload:
        raw_quizzes = payload["quizzes"]
    elif isinstance(payload, list):
        raw_quizzes = payload
    else:
        raw_quizzes = [payload]
    if not isinstance(raw_quizzes, list) or not 1 <= len(raw_quizzes) <= 50:
        raise core.HTTPException(
            status_code=422, detail="Import 1 to 50 quizzes at a time."
        )
    imported = []
    for raw_quiz in raw_quizzes:
        if not isinstance(raw_quiz, dict):
            raise core.HTTPException(
                status_code=422, detail="Each imported quiz must be a JSON object."
            )
        try:
            body = core.QuizBody(**raw_quiz)
        except core.ValidationError:
            raise core.HTTPException(
                status_code=422, detail="Imported quiz has an invalid structure."
            ) from None
        imported.append(
            core._build_quiz(body.title, body.duration_minutes, body.questions)
        )
    for quiz in imported:
        core._write_quiz(quiz, teacher_id)
    return {
        "imported_count": len(imported),
        "quizzes": [core._public_quiz(quiz) for quiz in imported],
    }


@router.delete("/api/teacher/quizzes/{quiz_id}")
def delete_quiz(quiz_id: str, request: core.Request) -> dict[str, bool]:
    teacher_id = core._require_teacher(request)
    path = core._quiz_path(quiz_id, teacher_id)
    if not path.exists():
        raise core.HTTPException(status_code=404, detail="Quiz not found.")
    if core._quiz_has_active_room(quiz_id, teacher_id):
        raise core.HTTPException(
            status_code=409,
            detail="Finish the active room for this quiz before deleting it.",
        )
    path.unlink()
    return {"ok": True}
