import hashlib
import json
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server


class QuizRoomFlowTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.auth_file = self.root / "auth.json"
        self.database_file = self.root / "quizroom.db"
        self.quiz_directory = self.root / "tests" / "quizzes"
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


if __name__ == "__main__":
    unittest.main()