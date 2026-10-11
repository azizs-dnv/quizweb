import getpass
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path


AUTH_FILE = Path(__file__).resolve().parent / "auth.json"


def main() -> None:
    if AUTH_FILE.exists():
        print(f"An account is already configured: {AUTH_FILE}")
        print("To change the password, remove auth.json and run this script again.")
        return

    print("QuizRoom initial setup. Your password will not be shown as you type.")
    username = input("Teacher username: ").strip()
    if not username or len(username) > 80:
        print("Username must contain between 1 and 80 characters.", file=sys.stderr)
        raise SystemExit(1)

    password = getpass.getpass("Password (at least 10 characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if len(password) < 10:
        print("Use a password with at least 10 characters.", file=sys.stderr)
        raise SystemExit(1)
    if password != confirmation:
        print("The passwords do not match.", file=sys.stderr)
        raise SystemExit(1)

    salt = secrets.token_bytes(16)
    password_hash = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    data = {
        "username": username,
        "password_salt": salt.hex(),
        "password_hash": password_hash.hex(),
        "token_secret": secrets.token_hex(32),
    }
    temporary = AUTH_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(AUTH_FILE)
    print(f"Done. Account credentials were saved to {AUTH_FILE.name}.")


if __name__ == "__main__":
    main()