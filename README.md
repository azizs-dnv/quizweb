# QuizRoom

QuizRoom is a local classroom quiz system. The teacher runs the server on their computer, and students join from phones or computers using a browser on the same Wi-Fi/LAN network. Students do not need to install anything.

The project uses Python, FastAPI, WebSockets, SQLite, and plain HTML/CSS/JavaScript. No frontend build step is required.

> GitHub stores the project source code but does not run this server. GitHub Pages cannot host QuizRoom; the app needs an always-on teacher computer or a separately configured Python server.

## Features

- Teacher sign-in with a password stored as a `scrypt` hash.
- Create and delete quizzes with a total duration and 2–12 answer options per question.
- Waiting rooms with six-digit join codes and a live participant list.
- A randomized question and answer-option order for each student.
- A shared room timer, speed-based points, a live leaderboard, and grades from 0 to 10.
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
3. Select **New quiz**, then enter a title and total duration.
4. Add questions and answer options, and mark the correct option. Every question needs at least two options.
5. Save the quiz and select **Launch** next to it.
6. Share the room code with the class. When students have joined, select **Start quiz**.
7. Monitor participant statuses and the leaderboard. Download the TXT results when the quiz ends.

Students select **Student** on the home page, then enter their name and the room code. The teacher must start the room before the quiz begins. If a student refreshes the page, select **Resume** on the home page to continue; saved answers and points remain on the server.

## Connect students over Wi-Fi/LAN

1. Connect the teacher computer and student devices to the same Wi-Fi network. Some guest networks block communication between devices.
2. Find the teacher computer's local IPv4 address:
   - Linux: run `hostname -I` and choose the Wi-Fi/LAN interface address.
   - Windows: run `ipconfig` and find the IPv4 address of the active Wi-Fi or Ethernet adapter.
3. If prompted by the firewall, allow incoming connections for Python. If necessary, allow TCP port `8000` on the local network.
4. Students open `http://TEACHER_ADDRESS:8000` in a browser. For example, if the teacher's address is `192.168.1.25`, use `http://192.168.1.25:8000`.
5. The teacher shares the room code shown in the dashboard.

The local IP address can change after reconnecting to the network. If the page cannot be reached, check that the server is running, the IP address is current, all devices are on the same network, and the firewall allows the port.

## Publish the project to GitHub

The repository has not been created yet, and this project has not been uploaded. When you are ready:

1. Sign in to GitHub as `azizs-dnv` and select **New repository**.
2. Name the repository `quizweb` and choose whether it should be public or private.
3. Do not initialize it with a README, `.gitignore`, or license; those files are already prepared in this project.
4. Open a terminal in the local project directory, `myquizapp`, and run:

```bash
git init
git add .
git status
```

Review the `git status` output before continuing. It must not include `auth.json`, `quizroom.db`, `.venv`, or `__pycache__`; `.gitignore` excludes those local files. Do not commit real student results or quizzes containing confidential material.

5. Create the first commit and connect it to the GitHub repository:

```bash
git commit -m "Initial QuizRoom project"
git branch -M main
git remote add origin https://github.com/azizs-dnv/quizweb.git
git push -u origin main
```

6. Refresh the repository page on GitHub and confirm that the files are there. This uploads the source code only; it does not publish or run the QuizRoom server on GitHub.

If Git reports that `origin` already exists, check `git remote -v` before changing it. For later updates, use:

```bash
git add .
git commit -m "Describe your changes"
git push
```

## Data and backups

- `auth.json` contains the teacher username, password salt and hash, and session signing key.
- `tests/quizzes/*.json` contains created quizzes.
- `quizroom.db` contains rooms, answers, points, and attempt state in SQLite.

Data stays on the computer running the server. `auth.json` and the SQLite database are excluded from Git by `.gitignore`. Before moving the app to another computer, back up `auth.json`, `tests/quizzes`, and `quizroom.db` separately. Do not publish these files, account credentials, or student personal data.

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