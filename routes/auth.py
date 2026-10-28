from __future__ import annotations

from fastapi import APIRouter

import sys

core = sys.modules.get("server") or sys.modules.get("__main__")
if core is None:
    raise RuntimeError("The QuizRoom application module is not loaded.")

router = APIRouter()


@router.get("/api/config")
def config() -> dict[str, core.Any]:
    return {"admin_ready": core._read_auth() is not None}


@router.post("/api/auth/login")
def login(body: core.LoginBody):
    root_auth = core._read_auth()
    if not root_auth:
        raise core.HTTPException(
            status_code=409,
            detail="Run init_admin.py to create the teacher account first.",
        )
    account_entry = core._account_for_username(body.username)
    if account_entry is None:
        raise core.HTTPException(
            status_code=401, detail="Incorrect username or password."
        )
    teacher_id, auth = account_entry
    candidate = core.hashlib.scrypt(
        body.password.encode(),
        salt=bytes.fromhex(auth["password_salt"]),
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    ).hex()
    if not core.hmac.compare_digest(candidate, auth["password_hash"]):
        raise core.HTTPException(
            status_code=401, detail="Incorrect username or password."
        )
    token = core._sign_session(root_auth["token_secret"], teacher_id)
    result = core.JSONResponse(
        {"username": auth["username"], "teacher_id": teacher_id, "token": token}
    )
    result.set_cookie(
        core.COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=core.SESSION_MAX_AGE,
    )
    return result


@router.post("/api/auth/register", status_code=201)
def register_teacher(body: core.RegisterBody):
    username = body.username.strip()
    if not 3 <= len(username) <= 80:
        raise core.HTTPException(
            status_code=422, detail="Username must contain between 3 and 80 characters."
        )
    if len(body.password) < 10:
        raise core.HTTPException(
            status_code=422, detail="Password must contain at least 10 characters."
        )
    with core._REGISTRATION_LOCK:
        if core._account_for_username(username):
            raise core.HTTPException(
                status_code=409, detail="That username is already in use."
            )
        root_auth = core._read_auth()
        salt = core.secrets.token_bytes(16)
        account = {
            "username": username,
            "password_salt": salt.hex(),
            "password_hash": core.hashlib.scrypt(
                body.password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32
            ).hex(),
        }
        if root_auth is None:
            root_auth = account | {"token_secret": core.secrets.token_hex(32)}
            temporary_file = core.AUTH_FILE.with_suffix(".tmp")
            temporary_file.write_text(
                core.json.dumps(root_auth, indent=2), encoding="utf-8"
            )
            try:
                temporary_file.chmod(384)
            except OSError:
                pass
            temporary_file.replace(core.AUTH_FILE)
            teacher_id = core.DEFAULT_TEACHER_ID
        else:
            teacher_id = core.secrets.token_hex(12)
            account["created_at"] = core.time.time()
            account_directory = core.TEACHER_DATA_DIR / teacher_id
            account_directory.mkdir(parents=True, exist_ok=False)
            account_file = account_directory / "account.json"
            temporary_file = account_directory / "account.tmp"
            temporary_file.write_text(
                core.json.dumps(account, indent=2), encoding="utf-8"
            )
            try:
                temporary_file.chmod(384)
            except OSError:
                pass
            temporary_file.replace(account_file)
    token = core._sign_session(root_auth["token_secret"], teacher_id)
    result = core.JSONResponse(
        {"username": username, "teacher_id": teacher_id, "token": token},
        status_code=201,
    )
    result.set_cookie(
        core.COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=core.SESSION_MAX_AGE,
    )
    return result


@router.post("/api/auth/logout")
def logout():
    result = core.JSONResponse({"ok": True})
    result.delete_cookie(core.COOKIE_NAME)
    return result
