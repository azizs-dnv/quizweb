import hashlib
import json
import secrets
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import server


class QuizRoomFlowTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.auth_file = self.root / "auth.json"
        self.database_file = self.root / "quizroom.db"
        self.quiz_directory = self.root / "tests" / "quizzes"
        self.teacher_data_directory = self.root / "teacher_data"
        self.quiz_directory.mkdir(parents=True)
        salt = secrets.token_bytes(16)
        password_hash = hashlib.scrypt(b"correct-horse-battery", salt=salt, n=2**14, r=8, p=1, dklen=32)
        self.auth_file.write_text(json.dumps({
            "username": "teacher",
            "password_salt": salt.hex(),
            "password_hash": password_hash.hex(),
            "token_secret": secrets.token_hex(32),
        }), encoding="utf-8")
        self.settings = patch.multiple(
            server,
            AUTH_FILE=self.auth_file,
            DB_FILE=self.database_file,
            QUIZ_DIR=self.quiz_directory,
            TEACHER_DATA_DIR=self.teacher_data_directory,
        )
        self.settings.start()
        server._init_database()
        self.client_context = TestClient(server.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.settings.stop()
        self.temporary_directory.cleanup()

    def test_teacher_can_create_room_and_save_student_result(self):
        login = self.client.post("/api/auth/login", json={"username": "teacher", "password": "correct-horse-battery"})
        self.assertEqual(login.status_code, 200)

        created = self.client.post("/api/teacher/quizzes", json={
            "title": "Test quiz",
            "duration_minutes": 5,
            "questions": [{"text": "What is 2 + 2?", "options": ["4", "5"], "correct_index": 0}],
        })
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["question_count"], 1)

        room = self.client.post(f"/api/teacher/rooms/{created.json()['id']}")
        self.assertEqual(room.status_code, 201, room.text)
        code = room.json()["code"]
        joined = self.client.post("/api/student/join", json={"code": code, "name": "Alex Morgan"})
        self.assertEqual(joined.status_code, 201, joined.text)
        self.assertEqual(joined.json()["participants"][0]["name"], "Alex Morgan")
        token = joined.json()["student"]["token"]

        started = self.client.post(f"/api/teacher/rooms/{code}/start")
        self.assertEqual(started.status_code, 200, started.text)
        attempt = self.client.post("/api/student/resume", json={"token": token}).json()
        question = attempt["questions"][0]
        answer = self.client.post("/api/student/answer", json={
            "token": token,
            "question_id": question["id"],
            "option_index": question["options"].index("4"),
        })
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertTrue(answer.json()["correct"])

        final_attempt = self.client.post("/api/student/resume", json={"token": token}).json()
        self.assertEqual(final_attempt["student"]["correct_count"], 1)
        self.assertEqual(final_attempt["student"]["answers"], [question["id"]])
        self.assertEqual(final_attempt["student"]["rank"], 1)

        exported = self.client.get(f"/api/teacher/rooms/{code}/export")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("Alex Morgan", exported.text)

    def test_completed_student_receives_updated_class_leaderboard(self):
        self.client.post(
            "/api/auth/login",
            json={"username": "teacher", "password": "correct-horse-battery"},
        )
        quiz = self.client.post(
            "/api/teacher/quizzes",
            json={
                "title": "Live results",
                "duration_minutes": 5,
                "questions": [
                    {"text": "Choose A", "options": ["A", "B"], "correct_index": 0}
                ],
            },
        ).json()
        code = self.client.post(f"/api/teacher/rooms/{quiz['id']}").json()["code"]
        first_student = self.client.post(
            "/api/student/join", json={"code": code, "name": "First Student"}
        ).json()["student"]
        second_student = self.client.post(
            "/api/student/join", json={"code": code, "name": "Second Student"}
        ).json()["student"]
        self.client.post(f"/api/teacher/rooms/{code}/start")

        first_attempt = self.client.post(
            "/api/student/resume", json={"token": first_student["token"]}
        ).json()
        first_question = first_attempt["questions"][0]
        first_answer = self.client.post(
            "/api/student/answer",
            json={
                "token": first_student["token"],
                "question_id": first_question["id"],
                "option_index": first_question["options"].index("A"),
            },
        ).json()
        self.assertTrue(first_answer["finished"])
        first_live_result = self.client.post(
            "/api/student/resume", json={"token": first_student["token"]}
        ).json()
        self.assertEqual(first_live_result["status"], "running")
        self.assertFalse(first_live_result["results_final"])
        self.assertEqual(
            next(
                person["score"]
                for person in first_live_result["leaderboard"]
                if person["name"] == "Second Student"
            ),
            0,
        )

        second_attempt = self.client.post(
            "/api/student/resume", json={"token": second_student["token"]}
        ).json()
        second_question = second_attempt["questions"][0]
        second_answer = self.client.post(
            "/api/student/answer",
            json={
                "token": second_student["token"],
                "question_id": second_question["id"],
                "option_index": second_question["options"].index("A"),
            },
        ).json()
        self.assertTrue(second_answer["finished"])

        refreshed_result = self.client.post(
            "/api/student/resume", json={"token": first_student["token"]}
        ).json()
        self.assertEqual(refreshed_result["status"], "running")
        self.assertTrue(refreshed_result["results_final"])
        self.assertEqual(refreshed_result["student"]["score"], first_answer["score"])
        self.assertEqual(
            {person["name"] for person in refreshed_result["leaderboard"]},
            {"First Student", "Second Student"},
        )
        self.assertEqual(
            next(
                person["score"]
                for person in refreshed_result["leaderboard"]
                if person["name"] == "Second Student"
            ),
            second_answer["score"],
        )
        self.assertEqual(
            [person["rank"] for person in refreshed_result["leaderboard"]],
            [1, 2],
        )

    def test_all_question_types_are_graded_and_legacy_single_choice_still_works(self):
        self.client.post("/api/auth/login", json={"username": "teacher", "password": "correct-horse-battery"})
        created = self.client.post("/api/teacher/quizzes", json={
            "title": "Question type coverage",
            "duration_minutes": 8,
            "questions": [
                {"text": "Choose one", "options": ["Red", "Blue"], "correct_index": 0},
                {"type": "multiple_choice", "text": "Choose two", "options": ["A", "B", "C"], "correct_indices": [0, 2]},
                {"type": "text", "text": "Name the planet", "expected_answer": "Saturn"},
                {"type": "matching", "text": "Match the pairs", "pairs": [
                    {"left": "A", "right": "4"}, {"left": "B", "right": "2"},
                ]},
            ],
        })
        self.assertEqual(created.status_code, 201, created.text)
        self.assertNotIn("correct_indices", created.json())
        code = self.client.post(f"/api/teacher/rooms/{created.json()['id']}").json()["code"]
        joined = self.client.post("/api/student/join", json={"code": code, "name": "Sam Lee"}).json()
        token = joined["student"]["token"]
        self.client.post(f"/api/teacher/rooms/{code}/start")
        attempt = self.client.post("/api/student/resume", json={"token": token}).json()

        while "questions" in attempt:
            question = next(item for item in attempt["questions"] if not item["answered"])
            if question["type"] == "single_choice":
                answer = question["options"].index("Red")
            elif question["type"] == "multiple_choice":
                answer = [index for index, option in enumerate(question["options"]) if option in {"A", "C"}]
            elif question["type"] == "text":
                answer = "  sATURN  "
            else:
                answer = {
                    pair["key"]: question["match_options"].index({"A": "4", "B": "2"}[pair["left"]])
                    for pair in question["pairs"]
                }
            result = self.client.post("/api/student/answer", json={
                "token": token,
                "question_id": question["id"],
                "answer": answer,
            })
            self.assertEqual(result.status_code, 200, result.text)
            self.assertTrue(result.json()["correct"], question["type"])
            self.assertGreater(result.json()["points"], 0)
            attempt = self.client.post("/api/student/resume", json={"token": token}).json()

        self.assertEqual(attempt["student"]["correct_count"], 4)

    def test_correct_answer_points_decrease_with_question_response_time(self):
        fast_points = server._points_for_correct_answer(1, 60)
        slower_points = server._points_for_correct_answer(30, 60)
        self.assertGreater(fast_points, slower_points)
        self.assertEqual(server._points_for_correct_answer(60, 60), 300)

    def test_expired_room_finishes_and_completes_students_without_teacher_request(self):
        self.client.post("/api/auth/login", json={"username": "teacher", "password": "correct-horse-battery"})
        quiz = self.client.post("/api/teacher/quizzes", json={
            "title": "Timed quiz",
            "duration_minutes": 1,
            "questions": [{"text": "Question", "options": ["A", "B"], "correct_index": 0}],
        }).json()
        code = self.client.post(f"/api/teacher/rooms/{quiz['id']}").json()["code"]
        student = self.client.post("/api/student/join", json={"code": code, "name": "Taylor Student"}).json()
        token = student["student"]["token"]
        self.client.post(f"/api/teacher/rooms/{code}/start")
        with server._database() as connection:
            connection.execute("UPDATE rooms SET deadline = ? WHERE code = ?", (time.time() - 1, code))

        server._finish_expired_rooms()

        with server._database() as connection:
            room_status = connection.execute("SELECT status FROM rooms WHERE code = ?", (code,)).fetchone()[0]
            student_status = connection.execute("SELECT status FROM participants WHERE token = ?", (token,)).fetchone()[0]
        self.assertEqual(room_status, "finished")
        self.assertEqual(student_status, "completed")

    def test_late_student_can_join_running_quiz_but_gets_fewer_points(self):
        self.client.post("/api/auth/login", json={"username": "teacher", "password": "correct-horse-battery"})
        quiz = self.client.post("/api/teacher/quizzes", json={
            "title": "Late join test",
            "duration_minutes": 10,
            "questions": [{"text": "Choose A", "options": ["A", "B"], "correct_index": 0}],
        }).json()
        code = self.client.post(f"/api/teacher/rooms/{quiz['id']}").json()["code"]
        early_student = self.client.post("/api/student/join", json={"code": code, "name": "Early Student"}).json()
        self.client.post(f"/api/teacher/rooms/{code}/start")
        early_attempt = self.client.post(
            "/api/student/resume", json={"token": early_student["student"]["token"]}
        ).json()
        early_question = early_attempt["questions"][0]
        early_answer = self.client.post("/api/student/answer", json={
            "token": early_student["student"]["token"],
            "question_id": early_question["id"],
            "option_index": early_question["options"].index("A"),
        }).json()
        early_results = self.client.post(
            "/api/student/resume",
            json={"token": early_student["student"]["token"]},
        ).json()
        self.assertTrue(early_results["results_final"])

        late_start = time.time() - 300
        with server._database() as connection:
            connection.execute(
                "UPDATE rooms SET started_at = ?, deadline = ? WHERE code = ?",
                (late_start, time.time() + 300, code),
            )
        late_student = self.client.post("/api/student/join", json={"code": code, "name": "Late Student"})
        self.assertEqual(late_student.status_code, 201, late_student.text)
        self.assertEqual(late_student.json()["student"]["status"], "in_progress")
        self.assertFalse(late_student.json()["results_final"])
        late_attempt = self.client.post(
            "/api/student/resume", json={"token": late_student.json()["student"]["token"]}
        ).json()
        late_question = late_attempt["questions"][0]
        late_answer = self.client.post("/api/student/answer", json={
            "token": late_student.json()["student"]["token"],
            "question_id": late_question["id"],
            "option_index": late_question["options"].index("A"),
        }).json()
        self.assertTrue(late_answer["correct"])
        self.assertGreater(early_answer["points"], late_answer["points"])
        self.assertLess(late_answer["points"], early_answer["points"] * 0.6)

    def test_existing_database_is_migrated_without_losing_rooms(self):
        legacy_database = self.root / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_database)
        try:
            connection.execute(
                "CREATE TABLE rooms (code TEXT PRIMARY KEY, quiz_id TEXT NOT NULL, quiz_json TEXT NOT NULL, "
                "status TEXT NOT NULL, created_at REAL NOT NULL, started_at REAL, deadline REAL)"
            )
            connection.execute(
                "INSERT INTO rooms(code, quiz_id, quiz_json, status, created_at) VALUES (?, ?, ?, ?, ?)",
                ("123456", "legacyquiz", "{}", "finished", 1),
            )
            connection.execute(
                "CREATE TABLE participants (token TEXT PRIMARY KEY, room_code TEXT NOT NULL, name TEXT NOT NULL, "
                "question_order TEXT NOT NULL, option_orders TEXT NOT NULL, answers TEXT NOT NULL DEFAULT '{}', "
                "score INTEGER NOT NULL DEFAULT 0, correct_count INTEGER NOT NULL DEFAULT 0, "
                "warnings INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'waiting', "
                "joined_at REAL NOT NULL, last_seen REAL NOT NULL, finished_at REAL)"
            )
            connection.commit()
        finally:
            connection.close()
        with patch.object(server, "DB_FILE", legacy_database):
            server._init_database()
        connection = sqlite3.connect(legacy_database)
        try:
            room = connection.execute("SELECT owner_id FROM rooms WHERE code = '123456'").fetchone()
            participant_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(participants)").fetchall()
            }
        finally:
            connection.close()
        self.assertEqual(room[0], server.DEFAULT_TEACHER_ID)
        self.assertIn("question_times", participant_columns)

    def test_teacher_accounts_have_separate_quiz_folders_and_lists(self):
        self.client.post("/api/auth/login", json={"username": "teacher", "password": "correct-horse-battery"})
        default_quiz = self.client.post("/api/teacher/quizzes", json={
            "title": "Default teacher quiz",
            "duration_minutes": 5,
            "questions": [{"text": "Question", "options": ["A", "B"], "correct_index": 0}],
        }).json()
        default_room_code = self.client.post(
            f"/api/teacher/rooms/{default_quiz['id']}"
        ).json()["code"]

        registered = self.client.post("/api/auth/register", json={
            "username": "second-teacher",
            "password": "second-secure-password",
        })
        self.assertEqual(registered.status_code, 201, registered.text)
        teacher_id = registered.json()["teacher_id"]
        second_quiz = self.client.post("/api/teacher/quizzes", json={
            "title": "Second teacher quiz",
            "duration_minutes": 5,
            "questions": [{"text": "Question", "options": ["A", "B"], "correct_index": 0}],
        }).json()

        own_quizzes = self.client.get("/api/teacher/quizzes").json()
        self.assertEqual([quiz["title"] for quiz in own_quizzes], ["Second teacher quiz"])
        self.assertTrue((self.quiz_directory / f"{default_quiz['id']}.json").exists())
        self.assertTrue((self.teacher_data_directory / teacher_id / "quizzes" / f"{second_quiz['id']}.json").exists())
        self.assertEqual(self.client.get(f"/api/teacher/quizzes/{default_quiz['id']}").status_code, 404)
        self.assertEqual(self.client.get("/api/teacher/rooms").json(), [])
        self.assertEqual(self.client.post(f"/api/teacher/rooms/{default_room_code}/start").status_code, 404)
        self.assertEqual(self.client.get(f"/api/teacher/rooms/{default_room_code}/export").status_code, 404)

        logged_in = self.client.post("/api/auth/login", json={
            "username": "SECOND-TEACHER",
            "password": "second-secure-password",
        })
        self.assertEqual(logged_in.status_code, 200)
        self.client.post("/api/auth/login", json={"username": "teacher", "password": "correct-horse-battery"})
        default_quizzes = self.client.get("/api/teacher/quizzes").json()
        self.assertEqual([quiz["title"] for quiz in default_quizzes], ["Default teacher quiz"])

    def test_first_teacher_can_register_without_init_admin(self):
        self.auth_file.unlink()

        registered = self.client.post("/api/auth/register", json={
            "username": "first-teacher",
            "password": "first-secure-password",
        })
        self.assertEqual(registered.status_code, 201, registered.text)
        self.assertTrue(self.auth_file.exists())

        logged_in = self.client.post("/api/auth/login", json={
            "username": "first-teacher",
            "password": "first-secure-password",
        })
        self.assertEqual(logged_in.status_code, 200, logged_in.text)

    def test_import_edit_and_active_room_lock(self):
        self.client.post("/api/auth/login", json={"username": "teacher", "password": "correct-horse-battery"})
        imported = self.client.post("/api/teacher/quizzes/import", json={"quizzes": [
            {
                "title": "Imported quiz one",
                "duration_minutes": 5,
                "questions": [{"text": "One?", "options": ["A", "B"], "correct_index": 1}],
            },
            {
                "title": "Imported quiz two",
                "duration_minutes": 8,
                "questions": [{"type": "text", "text": "Name it", "expected_answer": "Earth"}],
            },
        ]})
        self.assertEqual(imported.status_code, 201, imported.text)
        self.assertEqual(imported.json()["imported_count"], 2)
        quiz_id = imported.json()["quizzes"][0]["id"]
        details = self.client.get(f"/api/teacher/quizzes/{quiz_id}")
        self.assertEqual(details.status_code, 200)
        self.assertEqual(details.json()["questions"][0]["correct_index"], 1)

        update_body = {
            "title": "Edited quiz",
            "duration_minutes": 6,
            "questions": [{"text": "Updated?", "options": ["Yes", "No"], "correct_index": 0}],
        }
        updated = self.client.put(f"/api/teacher/quizzes/{quiz_id}", json=update_body)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["title"], "Edited quiz")

        room = self.client.post(f"/api/teacher/rooms/{quiz_id}")
        self.assertEqual(room.status_code, 201, room.text)
        locked = self.client.put(f"/api/teacher/quizzes/{quiz_id}", json=update_body)
        self.assertEqual(locked.status_code, 409)
        self.client.post(f"/api/teacher/rooms/{room.json()['code']}/finish")
        editable_again = self.client.put(f"/api/teacher/quizzes/{quiz_id}", json=update_body)
        self.assertEqual(editable_again.status_code, 200, editable_again.text)


class StartupAddressTest(unittest.TestCase):
    def test_local_network_ip_returns_address_for_students(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.getsockname.return_value = ("192.168.1.25", 0)
        with patch.object(server.socket, "socket", return_value=connection):
            self.assertEqual(server._local_network_ip(), "192.168.1.25")

    def test_local_network_ip_returns_none_when_network_is_unavailable(self):
        with patch.object(
            server.socket, "socket", side_effect=OSError("Network unavailable")
        ):
            self.assertIsNone(server._local_network_ip())


if __name__ == "__main__":
    unittest.main()