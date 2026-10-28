# QuizRoom

QuizRoom is a local classroom quiz system. The teacher runs the server on their computer, and students join from phones or computers using a browser on the same Wi-Fi/LAN network. Students do not need to install anything.

The project uses Python, FastAPI, WebSockets, SQLite, and plain HTML/CSS/JavaScript. No frontend build step is required.

The server entry point is `server.py`. API endpoints are grouped in `routes/auth.py`, `routes/quizzes.py`, and `routes/rooms.py`; shared database, authentication, and quiz logic stays in `server.py`.

> GitHub stores the project source code but does not run this server. GitHub Pages cannot host QuizRoom; the app needs an always-on teacher computer or a separately configured Python server.

## Features

- Separate teacher accounts with passwords stored as `scrypt` hashes and private quiz folders.
- Create, edit, import, and delete single-choice, multiple-choice, open-text, and matching-pairs quizzes.
- Waiting rooms with six-digit join codes and a live participant list.
- Automatic room completion when the shared deadline expires, even if the teacher is away.
- Late students can join a running room until the deadline; their points scale down with the time already elapsed.
- A randomized question and answer-option order for each student.
- A shared room timer, per-question speed points, a live leaderboard, and grades from 0 to 10.
- Server-side attempt saving and resume after a page refresh.
- Focus-loss warnings; a second recorded warning ends the attempt early.
- Downloadable TXT results.
- Responsive interface with light and dark themes.

## Requirements

- Python 3.10 or newer on the computer that will run the server.
- Git to clone or publish the source code.
- The teacher computer and student devices must be able to communicate over the same local network.

Students only need a browser and the server address. Internet access is not required for the app itself, but may be needed to install Python packages or load web fonts.

## Clone the GitHub repository

The clone instructions below will work after the `azizs-dnv/quizweb` repository has been created and the project has been pushed to it. Repository URL:

```text
https://github.com/azizs-dnv/quizweb
```

Open a terminal in the directory where you want the project and run:

```bash
git clone https://github.com/azizs-dnv/quizweb.git
cd quizweb
```

GitHub will ask you to authenticate when cloning a private repository. Public repositories can usually be cloned without signing in.

## Install and run for the first time

Run the server on the teacher's computer. Run the setup commands from the `quizweb` directory (or from `myquizapp` if you are running the project before it is published).

### Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python init_admin.py
python server.py
```

If Python reports that `venv` is unavailable, install your distribution's `python3-venv` package and retry.

### Windows PowerShell

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python init_admin.py
python server.py
```

If PowerShell blocks `Activate.ps1`, activate the environment from Windows Command Prompt instead:

```bat
.venv\Scripts\activate.bat
```

The `init_admin.py` script asks you to create a username and a password with at least 10 characters. The password is not shown while you type. Setup creates `auth.json`; you only need to create the teacher account once on that computer. If the file already exists, the script will tell you.

After the server starts, the terminal reports that it is listening on port `8000`. Keep this terminal open during the lesson. Open the app on the teacher computer at:

```text
http://127.0.0.1:8000
```

Press `Ctrl+C` in the terminal to stop the server. To start it again, activate `.venv` and run `python server.py`.

## Run a quiz

1. Open the app on the teacher computer and select **Teacher**.
2. Sign in with the account created by `init_admin.py`.
3. To add another teacher, sign out, select **Teacher**, choose **Create a separate teacher account**, and register a unique username and password. Each teacher has private quizzes and rooms.
4. Select **New quiz**, then enter a title and total duration.
5. Add questions and answer options, and mark the correct option. Every choice question needs at least two options.
6. Save the quiz and select **Launch** next to it.
7. Share the room code with the class. Select **Start quiz** when ready. Students can also join after the quiz starts, until the deadline.
8. The quiz ends automatically when its total timer reaches zero. The teacher can also end it manually at any time.
9. Monitor participant statuses and the leaderboard. Download the TXT results when the quiz ends.

Students select **Student** on the home page, then enter their name and the room code. The teacher must start the room before the quiz begins. If a student refreshes the page, select **Resume** on the home page to continue; saved answers and points remain on the server.

### Question types and scoring

- **Single choice:** the student selects one option. Existing quizzes without a `type` field continue to work as single-choice quizzes.
- **Multiple choice:** the student selects every correct option. The answer is marked correct only when the selected set exactly matches the answer key.
- **Open text:** the teacher enters an expected answer. Automatic checking ignores letter case and repeated or leading/trailing spaces, but otherwise requires an exact match.
- **Question formatting:** line breaks are preserved, and standalone code assignments are highlighted automatically. For code inside a description, use a fenced block with a language name, such as ` ```python ` on its own line, then the code, then ` ``` ` on its own line.
- **Matching pairs:** the teacher creates 2–12 unique pairs. Each student receives a randomized left and right column and must match every item correctly.

Incorrect answers earn 0 points. A correct answer earns from 300 to 1,000 points based on how quickly it is submitted after that question appears. The expected response window is the total quiz duration divided by the number of questions; answering faster earns more points. Late joiners receive an additional multiplier based on the fraction of total quiz time remaining when they join. The overall quiz timer remains shared, with no separate per-question deadline.

### Import and edit quizzes

Select **Import JSON** on the teacher dashboard to choose one or more `.json` files. Each file can contain one quiz, an array of quizzes, or an object with a `quizzes` array. Imported quiz data uses the same question structure supported by the builder; quizzes without a question `type` are treated as single-choice for compatibility.

Select **Edit** next to a quiz to change its title, duration, questions, and answer keys. A quiz cannot be edited while its room is waiting or running. Finish the room first; completed rooms retain their saved quiz snapshot and results.

### Teacher quiz storage

- The teacher created by `init_admin.py` is the default account. Its existing quizzes remain in `tests/quizzes/`.
- Every additional teacher has an account record at `teacher_data/<teacher-id>/account.json` and quizzes at `teacher_data/<teacher-id>/quizzes/`.
- All of these files remain local and are excluded from Git. Back them up with `auth.json` and `quizroom.db` if you need to move the installation.

## Connect students over Wi-Fi/LAN

1. Connect the teacher computer and student devices to the same Wi-Fi network. Some guest networks block communication between devices.
2. Find the teacher computer's local IPv4 address:
   - Linux: run `hostname -I` and choose the Wi-Fi/LAN interface address.
   - Windows: run `ipconfig` and find the IPv4 address of the active Wi-Fi or Ethernet adapter.
3. If prompted by the firewall, allow incoming connections for Python. If necessary, allow TCP port `8000` on the local network.
4. Students open `http://TEACHER_ADDRESS:8000` in a browser. For example, if the teacher's address is `192.168.1.25`, use `http://192.168.1.25:8000`.
5. The teacher shares the room code shown in the dashboard.

The local IP address can change after reconnecting to the network. If the page cannot be reached, check that the server is running, the IP address is current, all devices are on the same network, and the firewall allows the port.

## Update the GitHub repository

The source repository is [github.com/azizs-dnv/quizweb](https://github.com/azizs-dnv/quizweb). From the local project directory, review the changes and run the tests before publishing:

```bash
git status --short
python -m unittest discover -s tests -v
git add .
git commit -m "Describe your changes"
git push
```

Before committing, confirm that `auth.json`, `quizroom.db`, `.venv`, `__pycache__`, `teacher_data/`, and local quiz JSON files are not staged. `.gitignore` excludes these data files. GitHub stores the source only; it does not run the QuizRoom server.

## Data and backups

- `auth.json` contains the default teacher username, password salt and hash, and session signing key.
- `tests/quizzes/*.json` contains quizzes owned by the default teacher.
- `teacher_data/<teacher-id>/` contains each additional teacher's account and quiz files.
- `quizroom.db` contains rooms, answers, points, and attempt state in SQLite.

The `rooms.owner_id` database field keeps each teacher's room list and controls scoped to its owner. Existing rooms are migrated to the `default` teacher when the server starts.

Data stays on the computer running the server. Account files, quiz JSON, and the SQLite database are excluded from Git by `.gitignore`. Before moving the app to another computer, back up `auth.json`, `tests/quizzes`, `teacher_data`, and `quizroom.db` separately. Do not publish these files, account credentials, or student personal data.

## Run the project tests

From the project root, with the packages from `requirements.txt` installed, run:

```bash
python -m unittest discover -s tests -v
```

## Limitations and security

The app is intended for a trusted local network. It uses HTTP without TLS; do not expose the server port to the public internet or use it on a public or untrusted Wi-Fi network.

The browser can report tab changes or loss of focus, but a web page cannot reliably detect whether Python, a code editor, or another program is open on the device. Full-screen mode also requires a student click due to browser restrictions. These features do not replace proctoring or guarantee that students cannot cheat.

## License

Copyright 2026 Aziz. All rights reserved except as granted by the license.

This project is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full text. This notice applies to the project materials owned by Aziz; third-party dependencies and assets remain subject to their own licenses.