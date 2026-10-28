const app = document.querySelector("#app");
const state = {
  role: "student",
  page: "home",
  authMode: "login",
  editingQuizId: null,
  quizzes: [],
  room: null,
  student: null,
  teacherToken: sessionStorage.getItem("teacherToken"),
  socket: null,
  warningAt: 0,
  timer: null,
  resultPoll: null,
  socketToken: null,
  serverOffset: 0,
};

const escapeHtml = (value = "") =>
  String(value).replace(
    /[&<>"']/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[char],
  );

function renderQuestionText(text = "") {
  const source = String(text).replace(/\r\n?/g, "\n");
  const codeFence = /```([^\n`]*)\n([\s\S]*?)```/g;
  let output = "";
  let cursor = 0;
  let match;

  const renderCode = (code, language = "") => {
    const normalizedLanguage = language.trim().toLowerCase();
    const languageClass = window.hljs?.getLanguage(normalizedLanguage)
      ? ` class="language-${escapeHtml(normalizedLanguage)}"`
      : "";
    return `<pre class="question-code"><code${languageClass}>${escapeHtml(code)}</code></pre>`;
  };
  const renderPlain = (value) => {
    const lines = value.trim().split("\n");
    const assignment = /^\s*(?:[A-Za-z_$][\w$]*\s*(?:=|:=)\s*.+|(?:#|\/\/).+|(?:print|return|console\.log)\s*\(.*\))\s*;?\s*$/;
    if (value.trim() && lines.every((line) => assignment.test(line))) {
      return renderCode(value.trim());
    }
    return value
      .split(/(`[^`\n]+`)/g)
      .map((part) => {
        if (part.startsWith("`") && part.endsWith("`")) {
          return `<code class="inline-code">${escapeHtml(part.slice(1, -1))}</code>`;
        }
        return escapeHtml(part).replace(/\n/g, "<br>");
      })
      .join("");
  };

  while ((match = codeFence.exec(source))) {
    output += renderPlain(source.slice(cursor, match.index));
    output += renderCode(match[2].replace(/\n$/, ""), match[1]);
    cursor = codeFence.lastIndex;
  }
  output += renderPlain(source.slice(cursor));
  return output;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = response.headers
    .get("content-type")
    ?.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok)
    throw new Error(
      data.detail || data || "The request could not be completed.",
    );
  return data;
}

function shell(content, footer = "") {
  return `<div class="shell">
    <header class="topbar">
      <a class="brand" href="#" data-action="home"><span class="brand-mark">Q</span> QuizRoom</a>
      <div class="top-actions">${footer}<button class="icon-button" data-action="theme" aria-label="Toggle theme" title="Toggle theme">◐</button></div>
    </header>${content}
  </div>`;
}

function showToast(message) {
  document.querySelector(".toast")?.remove();
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.textContent = message;
  document.body.append(toast);
  setTimeout(() => toast.remove(), 3400);
}

function roleTabs() {
  return `<div class="role-tabs" role="tablist" aria-label="Choose a role">
    <button class="role-tab ${state.role === "student" ? "active" : ""}" data-action="role" data-role="student">Student</button>
    <button class="role-tab ${state.role === "teacher" ? "active" : ""}" data-action="role" data-role="teacher">Teacher</button>
  </div>`;
}

function renderHome(message = "") {
  state.page = "home";
  closeSocket();
  const saved = localStorage.getItem("quizroomAttempt");
  const studentForm = `<form id="join-form">
    <div class="field"><label for="student-name">Your name</label><input class="input" id="student-name" name="name" maxlength="60" autocomplete="name" placeholder="For example, Alex Morgan" required></div>
    <div class="field"><label for="room-code">Room code</label><input class="input mono" id="room-code" name="code" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" placeholder="123456" required></div>
    <div class="form-error">${escapeHtml(message)}</div><button class="button" type="submit">Join room <span aria-hidden="true">→</span></button>
  </form>${saved ? `<div class="resume-box"><span class="muted">A saved attempt is available</span><button class="link-button" data-action="resume">Resume →</button></div>` : ""}`;
  const teacherForm = `<form id="login-form">
    <div class="field"><label for="username">Teacher username</label><input class="input" id="username" name="username" autocomplete="username" required></div>
    <div class="field"><label for="password">Password</label><input class="input" id="password" type="password" name="password" autocomplete="current-password" required></div>
    <div class="form-error">${escapeHtml(message)}</div><button class="button" type="submit">Sign in <span aria-hidden="true">→</span></button>
    <p class="caption">First time here? Create your teacher account here or run <span class="mono">python init_admin.py</span>.</p>
    <button class="link-button" type="button" data-action="auth-mode" data-mode="register">Create a teacher account</button>
  </form>`;
  const registerForm = `<form id="register-form">
    <div class="field"><label for="new-username">Teacher username</label><input class="input" id="new-username" name="username" minlength="3" maxlength="80" autocomplete="username" required></div>
    <div class="field"><label for="new-password">Password (at least 10 characters)</label><input class="input" id="new-password" name="password" type="password" minlength="10" autocomplete="new-password" required></div>
    <div class="form-error">${escapeHtml(message)}</div><button class="button" type="submit">Create account <span aria-hidden="true">→</span></button>
    <button class="link-button" type="button" data-action="auth-mode" data-mode="login">Back to sign in</button>
  </form>`;
  const teacherFormContent = state.authMode === "register" ? registerForm : teacherForm;
  const form = state.role === "student" ? studentForm : teacherFormContent;
  app.innerHTML = shell(`<section class="home-grid">
    <div class="intro"><p class="eyebrow">Classroom testing on your network</p><h1>Class is ready.<br><span>Game on.</span></h1><p class="intro-copy">Live quizzes, a class leaderboard, and results in one place. Teachers need a computer; students only need a browser and a room code.</p><div class="intro-note"><span class="pulse"></span> Runs on your local Wi-Fi</div></div>
    <section class="panel"><div class="panel-head"><div><p class="eyebrow">Enter QuizRoom</p><h2>${state.role === "student" ? "Join a room" : state.authMode === "register" ? "Create teacher account" : "Welcome back"}</h2></div></div>${roleTabs()}${form}</section>
  </section><p class="footer-note">Local quizzes · quick answers · live leaderboard</p>`);
}

function choiceOptionRow(questionId, index, type, value = "", isCorrect = false) {
  const control = type === "multiple_choice" ? "checkbox" : "radio";
  const checked = isCorrect || (type === "single_choice" && index === 0 && value === "") ? "checked" : "";
  return `<div class="option-edit-row"><input type="${control}" name="correct-${questionId}" value="${index}" ${checked} aria-label="Correct answer"><input type="text" class="option-text" maxlength="300" placeholder="Option ${index + 1}" value="${escapeHtml(value)}" required><button class="mini-icon" type="button" data-action="remove-option" title="Remove option" aria-label="Remove option">×</button></div>`;
}

function matchingPairRow(left = "", right = "", number = 1) {
  return `<div class="pair-edit-row"><span class="pair-number">${number}</span><input type="text" class="match-left" maxlength="300" placeholder="Left item ${number}" value="${escapeHtml(left)}" required><input type="text" class="match-right" maxlength="300" placeholder="Match ${number}" value="${escapeHtml(right)}" required><button class="mini-icon" type="button" data-action="remove-pair" title="Remove pair" aria-label="Remove pair">×</button></div>`;
}

function questionTypeFields(questionId, type = "single_choice", options = ["", ""], pairs = [{ left: "", right: "" }, { left: "", right: "" }], question = null, optionRows = null) {
  if (type === "text") {
    return `<div class="field"><label>Expected answer</label><input class="input expected-answer" maxlength="1000" placeholder="Enter the accepted answer" value="${escapeHtml(question?.expected_answer || "")}" required><p class="caption">Answers are checked automatically, ignoring case and extra spaces.</p></div>`;
  }
  if (type === "matching") {
    return `<span class="field-label">Create 2–12 unique pairs to match</span><div class="pair-editor">${pairs.map((pair, index) => matchingPairRow(pair.left, pair.right, index + 1)).join("")}</div><button class="link-button" type="button" data-action="add-pair">+ Add pair</button>`;
  }
  const guidance = type === "multiple_choice" ? "Select every correct option" : "Mark the one correct answer";
  return `<span class="field-label">Answer options · ${guidance.toLowerCase()}</span><div class="option-editor">${optionRows || options.map((value, index) => choiceOptionRow(questionId, index, type, value)).join("")}</div><button class="link-button add-option" type="button" data-action="add-option">+ Add option</button>`;
}

function questionEditor(number, question = null) {
  const questionId = `q-${crypto.randomUUID()}`;
  const type = question?.type || "single_choice";
  const options = question?.options || ["", ""];
  const pairs = question?.pairs || [{ left: "", right: "" }, { left: "", right: "" }];
  const optionRows = type === "multiple_choice"
    ? options.map((value, index) => choiceOptionRow(questionId, index, type, value, question.correct_indices.includes(index))).join("")
    : options.map((value, index) => choiceOptionRow(questionId, index, type, value, index === question?.correct_index)).join("");
  return `<article class="question-card" data-question-id="${questionId}">
    <div class="question-top"><strong>QUESTION ${number}</strong><button class="mini-icon" type="button" data-action="remove-question" title="Remove question" aria-label="Remove question">×</button></div>
    <div class="field"><label>Question</label><textarea class="textarea question-text" maxlength="1000" placeholder="Enter a question or paste code" required>${escapeHtml(question?.text || "")}</textarea><p class="caption">Line breaks are kept. For code inside a description, wrap it with triple backticks and a language, for example python.</p></div>
    <div class="field"><label>Question type</label><select class="select question-type"><option value="single_choice" ${type === "single_choice" ? "selected" : ""}>Single choice</option><option value="multiple_choice" ${type === "multiple_choice" ? "selected" : ""}>Multiple choice</option><option value="text" ${type === "text" ? "selected" : ""}>Open text</option><option value="matching" ${type === "matching" ? "selected" : ""}>Matching pairs</option></select></div>
    <div class="type-fields">${questionTypeFields(questionId, type, options, pairs, question, optionRows)}</div>
  </article>`;
}

function studentAnswerControls(question) {
  if (question.type === "text") {
    return `<form class="student-answer-form" id="text-answer-form" data-question-id="${escapeHtml(question.id)}"><div class="field"><label for="student-text-answer">Your answer</label><input class="input" id="student-text-answer" name="answer" maxlength="1000" autocomplete="off" required></div><button class="button" type="submit">Submit answer →</button></form>`;
  }
  if (question.type === "multiple_choice") {
    return `<form class="student-answer-form" id="multiple-answer-form" data-question-id="${escapeHtml(question.id)}"><div class="answer-grid">${question.options.map((option, index) => `<label class="answer-option multi-answer-option"><input type="checkbox" name="option" value="${index}"><span>${escapeHtml(option)}</span></label>`).join("")}</div><button class="button" type="submit">Submit answers →</button></form>`;
  }
  if (question.type === "matching") {
    return `<form class="student-answer-form" id="matching-answer-form" data-question-id="${escapeHtml(question.id)}"><div class="matching-answer-list">${question.pairs.map((pair) => `<label class="matching-answer-row"><span>${escapeHtml(pair.left)}</span><select class="select matching-select" data-left-key="${pair.key}" required><option value="">Choose a match</option>${question.match_options.map((option, index) => `<option value="${index}">${escapeHtml(option)}</option>`).join("")}</select></label>`).join("")}</div><button class="button" type="submit">Check matches →</button></form>`;
  }
  return `<div class="answer-grid">${question.options.map((option, index) => `<button class="answer-option" data-action="answer" data-question="${escapeHtml(question.id)}" data-option="${index}">${escapeHtml(option)}</button>`).join("")}</div>`;
}

async function submitStudentAnswer(questionId, answer) {
  const controls = document.querySelectorAll(".student-answer-form button, .answer-option");
  controls.forEach((control) => { control.disabled = true; });
  try {
    const token = state.student.token;
    const result = await api("/api/student/answer", {
      method: "POST",
      body: { token, question_id: questionId, answer },
    });
    const feedback = document.querySelector("#answer-feedback");
    if (feedback) {
      feedback.className = `answer-feedback ${result.correct ? "good" : "bad"}`;
      feedback.textContent = result.correct
        ? `Correct · +${result.points} points`
        : "Not quite. Your answer is saved.";
    }
    setTimeout(async () => {
      try {
        const room = await api("/api/student/resume", {
          method: "POST",
          body: { token },
        });
        loadStudentRoom(room, room.student);
      } catch (error) {
        showToast(error.message);
      }
    }, 850);
  } catch (error) {
    controls.forEach((control) => { control.disabled = false; });
    throw error;
  }
}

function renderBuilder(quiz = null) {
  state.page = "builder";
  state.editingQuizId = quiz?.id || null;
  closeSocket();
  app.innerHTML = shell(`<section class="builder">
    <div class="builder-head"><div><p class="eyebrow">Quiz builder</p><h1>${quiz ? "Edit quiz" : "New quiz"}</h1></div><button class="button secondary small" data-action="dashboard">← Back to quizzes</button></div>
    <form id="quiz-form"><div class="panel">
      <div class="field"><label for="quiz-title">Quiz title</label><input class="input" id="quiz-title" name="title" maxlength="120" placeholder="For example, Geometry: triangles" value="${escapeHtml(quiz?.title || "")}" required></div>
      <div class="field"><label for="quiz-duration">Total duration, minutes</label><input class="input" id="quiz-duration" name="duration" type="number" min="1" max="240" value="${quiz?.duration_minutes || 10}" required></div>
      <div class="section-title"><h2>Questions</h2><span class="muted" id="question-count">${quiz?.questions.length || 1} ${(quiz?.questions.length || 1) === 1 ? "question" : "questions"}</span></div>
      <div class="question-list" id="question-list">${quiz ? quiz.questions.map((question, index) => questionEditor(index + 1, question)).join("") : questionEditor(1)}</div>
      <div class="form-error" id="builder-error"></div><div class="toolbar"><button class="button secondary" type="button" data-action="add-question">+ Add question</button><button class="button" type="submit">${quiz ? "Save changes" : "Save quiz"} →</button></div>
    </div></form>
  </section>`);
}

function renderTeacherDashboard() {
  state.page = "teacher";
  const quizRows = state.quizzes.length
    ? state.quizzes
        .map(
          (quiz) =>
            `<article class="quiz-row"><div><h3>${escapeHtml(quiz.title)}</h3><p>${quiz.question_count} questions · ${quiz.duration_minutes} min${quiz.active ? " · Active room" : ""}</p></div><div class="row-actions"><button class="button secondary small" data-action="edit-quiz" data-id="${escapeHtml(quiz.id)}" ${quiz.active ? "disabled title=\"Finish the active room before editing\"" : ""}>Edit</button><button class="button small" data-action="create-room" data-id="${escapeHtml(quiz.id)}">Launch</button><button class="mini-icon" data-action="delete-quiz" data-id="${escapeHtml(quiz.id)}" title="Delete quiz" aria-label="Delete quiz">×</button></div></article>`,
        )
        .join("")
    : `<div class="empty-state">No quizzes yet. Create your first quiz to open a room.</div>`;
  app.innerHTML =
    shell(`<div class="dashboard-head"><div><p class="eyebrow">Teacher dashboard</p><h1>Manage your class</h1></div><div class="row-actions"><input id="quiz-import-input" type="file" accept=".json,application/json" multiple hidden><button class="button secondary" data-action="import-quizzes">Import JSON</button><button class="button secondary small" data-action="logout">Sign out</button><button class="button" data-action="builder">+ New quiz</button></div></div>
    <div id="student-join-code"></div><div id="room-live"></div><div class="dashboard-layout"><section><h2 class="section-title">Your quizzes <span class="mono muted">${state.quizzes.length}</span></h2><div class="quiz-list">${quizRows}</div></section>
    <aside class="panel"><p class="eyebrow">How it works</p><h2>One room<br>for the whole class</h2><p class="muted">Create a quiz, share its six-digit code, wait for students, then start the shared timer.</p><p class="caption">Results are saved on this computer and can be downloaded as a TXT file.</p></aside></div>`);
  updateJoinCodeBanner();
  updateRoomPanel();
  connectTeacherSocket();
}

function updateJoinCodeBanner() {
  const host = document.querySelector("#student-join-code");
  if (!host) return;
  const room = state.room;
  const activeRoom = room && ["waiting", "running"].includes(room.status);
  host.innerHTML = activeRoom
    ? `<section class="student-join-banner"><div><p class="eyebrow">STUDENTS ENTER THIS ROOM CODE</p><div class="student-join-code">${escapeHtml(room.code)}</div><p class="room-meta">Students enter this code on the home page to join the quiz.</p></div><button class="button secondary" data-action="copy-room-code" data-code="${escapeHtml(room.code)}">Copy code</button></section>`
    : `<section class="student-join-banner empty"><div><p class="eyebrow">STUDENT ROOM CODE</p><p class="room-meta">Launch a quiz to create a room code for students.</p></div></section>`;
}

function updateRoomPanel() {
  const host = document.querySelector("#room-live");
  if (!host) return;
  updateJoinCodeBanner();
  if (!state.room) return;
  const room = state.room;
  const people = room.participants || room.leaderboard || [];
  const statusText =
    { waiting: "Waiting", running: "In progress", finished: "Finished" }[
      room.status
    ] || room.status;
  const peopleHtml = people.length
    ? people
        .map(
          (person) =>
            `<div class="participant-row"><span>${escapeHtml(person.name)}</span><span class="status ${escapeHtml(person.status)}">${escapeHtml({ waiting: "In lobby", in_progress: "Answering", completed: "Finished", disconnected: "Offline", blocked: "Attempt stopped" }[person.status] || person.status)}</span><span class="mono">${person.score} pts</span></div>`,
        )
        .join("")
    : `<div class="empty-state">No students have joined yet</div>`;
  host.innerHTML = `<section class="panel room-live"><div class="room-bar"><div><p class="eyebrow">${escapeHtml(room.quiz.title)} · ${statusText}</p><div class="room-meta">Participants: ${people.length} · ${room.quiz.duration_minutes} minutes</div></div><div class="row-actions">${room.status === "waiting" ? `<button class="button" data-action="start-room" data-code="${room.code}" ${people.length ? "" : "disabled"}>Start quiz →</button>` : ""}${room.status !== "finished" ? `<button class="button danger small" data-action="finish-room" data-code="${room.code}">End quiz</button>` : `<a class="button secondary small" href="/api/teacher/rooms/${room.code}/export">Download TXT ↓</a>`}</div></div><div class="participant-list">${peopleHtml}</div>${room.status === "running" ? `<h3 class="section-title" style="margin-top:24px">Live leaderboard</h3>${leaderboardHtml(room.leaderboard)}` : ""}</section>`;
}

function leaderboardHtml(board = []) {
  if (!board.length)
    return `<div class="empty-state">The leaderboard will appear after students answer</div>`;
  return `<div class="leaderboard">${board.map((person) => `<div class="leaderboard-row"><span class="leaderboard-rank">#${person.rank}</span><strong>${escapeHtml(person.name)}</strong><span class="mono">${person.score} · ${person.grade}/10</span></div>`).join("")}</div>`;
}

function updateStudentResult() {
  if (!state.room || !state.student || state.page !== "student-result") return;
  const { room, student } = { room: state.room, student: state.student };
  const score = document.querySelector("#student-result-score");
  if (!score) return;
  score.innerHTML = `${student.score} <span style="font-size:20px">points</span>`;
  document.querySelector("#student-result-rank").textContent =
    `Place: #${student.rank || "—"}`;
  document.querySelector("#student-result-correct").textContent =
    `Correct: ${student.correct_count} of ${room.quiz.question_count}`;
  const grade = room.quiz.question_count
    ? ((student.correct_count / room.quiz.question_count) * 10).toFixed(1)
    : "0";
  document.querySelector("#student-result-grade").textContent =
    `Grade: ${grade} / 10`;
  document.querySelector("#student-result-warnings").textContent =
    `Warnings: ${student.warnings}`;
  document.querySelector("#student-result-live-status").textContent =
    room.results_final
      ? "Final class results."
      : "Live points — final places appear when everyone finishes.";
  document.querySelector("#student-result-podium").innerHTML =
    room.results_final
      ? room.leaderboard
          .slice(0, 3)
          .map(
            (person) =>
              `<p>${person.rank}. ${escapeHtml(person.name)} — ${person.score} pts</p>`,
          )
          .join("")
      : "";
  document.querySelector("#student-result-leaderboard").innerHTML =
    leaderboardHtml(room.leaderboard);
}

function startStudentResultPolling(token) {
  clearInterval(state.resultPoll);
  let requestPending = false;
  const refresh = async () => {
    if (
      requestPending ||
      state.page !== "student-result" ||
      state.student?.token !== token
    )
      return;
    requestPending = true;
    try {
      const room = await api("/api/student/resume", {
        method: "POST",
        body: { token },
      });
      if (state.page !== "student-result" || state.student?.token !== token)
        return;
      state.room = room;
      state.student = room.student;
      updateStudentResult();
      if (room.status === "finished") closeSocketForRender();
    } catch (error) {
      const status = document.querySelector("#student-result-live-status");
      if (status)
        status.textContent = `Connection interrupted; retrying results update. ${error.message}`;
    } finally {
      requestPending = false;
    }
  };
  refresh();
  state.resultPoll = setInterval(refresh, 1000);
}

function closeSocket() {
  if (state.socket) {
    state.socket.onclose = null;
    state.socket.close();
    state.socket = null;
  }
  state.socketToken = null;
  clearInterval(state.timer);
  state.timer = null;
  clearInterval(state.resultPoll);
  state.resultPoll = null;
}

function connectTeacherSocket() {
  closeSocket();
  const socket = new WebSocket(
    `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`,
  );
  state.socket = socket;
  socket.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type !== "teacher") return;
    const wanted = sessionStorage.getItem("activeRoom");
    const room =
      data.rooms.find((item) => item.code === wanted) || data.rooms[0];
    if (room) {
      state.room = room;
      sessionStorage.setItem("activeRoom", room.code);
    }
    updateRoomPanel();
  };
  socket.onclose = () => {
    if (state.page === "teacher") setTimeout(connectTeacherSocket, 1800);
  };
}

function connectStudentSocket() {
  const token = state.student?.token;
  if (!token) return;
  if (
    state.socket &&
    state.socketToken === token &&
    state.socket.readyState < WebSocket.CLOSING
  )
    return;
  closeSocket();
  state.socketToken = token;
  const socket = new WebSocket(
    `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws?token=${encodeURIComponent(token)}`,
  );
  state.socket = socket;
  socket.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type !== "student") return;
    const previousRoomStatus = state.room?.status;
    const previousStudentStatus = state.student?.status;
    const previousAnswers = JSON.stringify(state.student?.answers || []);
    state.room = data.room;
    state.serverOffset = data.room.server_time - Date.now() / 1000;
    state.student = data.room.student;
    localStorage.setItem(
      "quizroomAttempt",
      JSON.stringify({ token: state.student.token, code: state.room.code }),
    );
    if (state.page === "student-result") {
      updateStudentResult();
      if (state.room.status === "finished") closeSocketForRender();
      return;
    }
    const answersChanged = previousAnswers !== JSON.stringify(state.student.answers || []);
    const answerPending = document.querySelector(
      ".student-answer-form button:disabled, .answer-option:disabled",
    );
    if (
      state.page !== "student-room" ||
      previousRoomStatus !== state.room.status ||
      previousStudentStatus !== state.student.status ||
      (answersChanged && !answerPending)
    ) {
      renderStudentRoom();
    } else {
      updateTimer();
    }
  };
  socket.onclose = () => {
    if (
      state.page === "student-room" ||
      (state.page === "student-result" && state.room?.status !== "finished")
    )
      setTimeout(connectStudentSocket, 1800);
  };
  state.timer = setInterval(() => {
    if (state.page === "student-room") updateTimer();
    api("/api/student/ping", { method: "POST", body: { token } }).catch(
      () => {},
    );
  }, 5000);
}

function updateTimer() {
  const target = document.querySelector("#countdown");
  if (!target || !state.room?.deadline) return;
  const remaining = Math.max(
    0,
    Math.ceil(state.room.deadline - (Date.now() / 1000 + state.serverOffset)),
  );
  target.textContent = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`;
}

function renderStudentRoom() {
  if (!state.room || !state.student) return;
  const { room, student } = { room: state.room, student: state.student };
  if (room.status === "waiting") {
    state.page = "student-lobby";
    const roster = room.participants
      .map(
        (person) =>
          `<div class="participant-row"><span>${escapeHtml(person.name)}</span><span class="status waiting">In lobby</span></div>`,
      )
      .join("");
    app.innerHTML = shell(
      `<section class="lobby-center"><p class="eyebrow">Room ${escapeHtml(room.code)}</p><h1>You're in, ${escapeHtml(student.name)}.</h1><p class="muted">Your questions will appear here when the teacher starts the quiz. You can enter full-screen mode now.</p><button class="button" data-action="fullscreen">I'm ready · Full screen</button><div class="panel" style="margin-top:28px;text-align:left"><h2 class="section-title">Class is joining <span class="mono muted">${room.participants.length}</span></h2><div class="participant-list">${roster || `<p class="muted">Loading participants…</p>`}</div></div></section>`,
    );
    connectStudentSocket();
    return;
  }
  if (
    room.status === "finished" ||
    student.status === "completed" ||
    student.status === "blocked"
  ) {
    state.page = "student-result";
    const title =
      student.status === "blocked" ? "Attempt ended early" : "Quiz complete";
    app.innerHTML = shell(
      `<section class="result-center"><p class="eyebrow">${escapeHtml(room.quiz.title)}</p><h1>${title}, ${escapeHtml(student.name)}.</h1><div class="result-score" id="student-result-score"></div><div class="result-stats"><span class="stat-chip" id="student-result-rank"></span><span class="stat-chip" id="student-result-correct"></span><span class="stat-chip" id="student-result-grade"></span><span class="stat-chip" id="student-result-warnings"></span></div><p class="muted" id="student-result-live-status"></p><div id="student-result-podium"></div><h2 class="section-title">Class leaderboard</h2><div id="student-result-leaderboard"></div><button class="button secondary" data-action="home">Home</button></section>`,
    );
    updateStudentResult();
    if (room.status === "finished") {
      closeSocketForRender();
    } else {
      connectStudentSocket();
      startStudentResultPolling(student.token);
    }
    return;
  }
  state.page = "student-room";
  const questions = room.questions || [];
  const answered = new Set(student.answers || []);
  const question = questions.find((item) => !answered.has(item.id));
  if (!question) {
    app.innerHTML = shell(
      `<section class="lobby-center"><p class="eyebrow">Answers saved</p><h1>Nice work.</h1><p class="muted">Your results will appear when the other participants finish.</p><div class="result-score">${student.score}</div></section>`,
    );
    connectStudentSocket();
    return;
  }
  const number = questions.findIndex((item) => item.id === question.id) + 1;
  app.innerHTML = shell(
    `<div class="student-top"><div class="student-progress"><span class="mono">${number} / ${questions.length}</span><div class="progress-track"><div class="progress-fill" style="width:${(answered.size / questions.length) * 100}%"></div></div></div><div class="timer" id="countdown">--:--</div></div><section class="student-stage"><span class="question-kicker">${escapeHtml(room.quiz.title)} · ${escapeHtml(student.name)}</span><div class="question-prompt" role="heading" aria-level="1">${renderQuestionText(question.text)}</div>${studentAnswerControls(question)}<div class="answer-feedback" id="answer-feedback"></div></section>`,
  );
  if (window.hljs) {
    document.querySelectorAll(".question-code code").forEach((code) => {
      window.hljs.highlightElement(code);
    });
  }
  updateTimer();
  connectStudentSocket();
}

function closeSocketForRender() {
  if (state.socket) {
    state.socket.onclose = null;
    state.socket.close();
    state.socket = null;
  }
  clearInterval(state.timer);
  state.timer = null;
  clearInterval(state.resultPoll);
  state.resultPoll = null;
}

async function loadTeacher() {
  try {
    state.quizzes = await api("/api/teacher/quizzes");
    const rooms = await api("/api/teacher/rooms");
    const wanted = sessionStorage.getItem("activeRoom");
    state.room = rooms.find((item) => item.code === wanted) || rooms[0] || null;
    if (state.room) sessionStorage.setItem("activeRoom", state.room.code);
    renderTeacherDashboard();
  } catch (error) {
    state.teacherToken = null;
    sessionStorage.removeItem("teacherToken");
    renderHome(error.message);
  }
}

function loadStudentRoom(room, student) {
  state.room = room;
  state.student = student;
  state.serverOffset = room.server_time - Date.now() / 1000;
  localStorage.setItem(
    "quizroomAttempt",
    JSON.stringify({ token: student.token, code: room.code }),
  );
  renderStudentRoom();
}

async function resumeStudent() {
  const saved = JSON.parse(localStorage.getItem("quizroomAttempt") || "null");
  if (!saved?.token) return renderHome("No saved attempt was found.");
  try {
    const room = await api("/api/student/resume", {
      method: "POST",
      body: { token: saved.token },
    });
    loadStudentRoom(room, room.student);
  } catch (error) {
    localStorage.removeItem("quizroomAttempt");
    renderHome(error.message);
  }
}

function updateQuestionCount() {
  const count = document.querySelectorAll(".question-card").length;
  const target = document.querySelector("#question-count");
  if (target)
    target.textContent = `${count} ${count === 1 ? "question" : "questions"}`;
}

function flagFocusLoss() {
  if (
    state.page !== "student-room" ||
    state.room?.status !== "running" ||
    !state.student?.token
  )
    return;
  const now = Date.now();
  if (now - state.warningAt < 1600) return;
  state.warningAt = now;
  api("/api/student/warning", {
    method: "POST",
    body: { token: state.student.token },
  })
    .then((result) => {
      if (result.blocked)
        showToast("You left the quiz again. Your attempt has ended.");
      else showToast("Warning: do not leave the quiz window.");
    })
    .catch(() => {});
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) flagFocusLoss();
});
window.addEventListener("blur", () => {
  if (document.activeElement instanceof HTMLSelectElement) return;
  flagFocusLoss();
});

document.addEventListener("change", (event) => {
  if (event.target.id === "quiz-import-input") {
    const files = [...event.target.files];
    if (!files.length) return;
    Promise.all(files.map(async (file) => JSON.parse(await file.text())))
      .then(async (documents) => {
        const quizzes = documents.flatMap((document) =>
          Array.isArray(document) ? document : Array.isArray(document.quizzes) ? document.quizzes : [document],
        );
        const result = await api("/api/teacher/quizzes/import", {
          method: "POST",
          body: { quizzes },
        });
        showToast(`Imported ${result.imported_count} ${result.imported_count === 1 ? "quiz" : "quizzes"}.`);
        await loadTeacher();
      })
      .catch((error) => showToast(`Import failed: ${error.message}`))
      .finally(() => { event.target.value = ""; });
    return;
  }
  const selector = event.target.closest(".question-type");
  if (!selector) return;
  const card = selector.closest(".question-card");
  card.querySelector(".type-fields").innerHTML = questionTypeFields(
    card.dataset.questionId,
    selector.value,
  );
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  try {
    if (action === "theme") {
      const next = document.body.dataset.theme === "light" ? "dark" : "light";
      document.body.dataset.theme = next;
      localStorage.setItem("quizroomTheme", next);
    } else if (action === "role") {
      state.role = button.dataset.role;
      state.authMode = "login";
      renderHome();
    } else if (action === "home") {
      state.role = "student";
      renderHome();
    } else if (action === "resume") {
      await resumeStudent();
    } else if (action === "builder") {
      renderBuilder();
    } else if (action === "auth-mode") {
      state.authMode = button.dataset.mode;
      renderHome();
    } else if (action === "import-quizzes") {
      document.querySelector("#quiz-import-input")?.click();
    } else if (action === "edit-quiz") {
      const quiz = await api(`/api/teacher/quizzes/${button.dataset.id}`);
      renderBuilder(quiz);
    } else if (action === "dashboard") {
      await loadTeacher();
    } else if (action === "logout") {
      await api("/api/auth/logout", { method: "POST" });
      sessionStorage.removeItem("teacherToken");
      sessionStorage.removeItem("activeRoom");
      state.teacherToken = null;
      renderHome();
    } else if (action === "add-question") {
      const list = document.querySelector("#question-list");
      list.insertAdjacentHTML(
        "beforeend",
        questionEditor(list.children.length + 1),
      );
      updateQuestionCount();
    } else if (action === "remove-question") {
      const list = document.querySelector("#question-list");
      if (list.children.length > 1) {
        button.closest(".question-card").remove();
        [...list.children].forEach((item, index) => {
          item.querySelector("strong").textContent = `QUESTION ${index + 1}`;
        });
        updateQuestionCount();
      } else showToast("A quiz must have at least one question.");
    } else if (action === "add-option") {
      const card = button.closest(".question-card");
      const list = card.querySelector(".option-editor");
      if (list.children.length >= 12)
        return showToast("You can add up to 12 answer options.");
      const index = list.children.length;
      list.insertAdjacentHTML(
        "beforeend",
        choiceOptionRow(card.dataset.questionId, index, card.querySelector(".question-type").value),
      );
    } else if (action === "remove-option") {
      const list = button.closest(".option-editor");
      if (list.children.length <= 2)
        return showToast("Each question needs at least two answer options.");
      button.parentElement.remove();
      [...list.children].forEach((row, index) => {
        row.querySelector("input[type=radio], input[type=checkbox]").value = index;
      });
      const correctControl = list.querySelector("input[type=radio], input[type=checkbox]");
      if (correctControl.type === "radio" && !list.querySelector("input[type=radio]:checked"))
        correctControl.checked = true;
    } else if (action === "add-pair") {
      const list = button.previousElementSibling;
      if (list.children.length >= 12) return showToast("You can add up to 12 matching pairs.");
      list.insertAdjacentHTML("beforeend", matchingPairRow("", "", list.children.length + 1));
    } else if (action === "remove-pair") {
      const list = button.closest(".pair-editor");
      if (list.children.length <= 2) return showToast("Add at least two matching pairs.");
      button.closest(".pair-edit-row").remove();
      [...list.children].forEach((row, index) => {
        const number = index + 1;
        row.querySelector(".pair-number").textContent = number;
        row.querySelector(".match-left").placeholder = `Left item ${number}`;
        row.querySelector(".match-right").placeholder = `Match ${number}`;
      });
    } else if (action === "create-room") {
      state.room = await api(`/api/teacher/rooms/${button.dataset.id}`, {
        method: "POST",
      });
      sessionStorage.setItem("activeRoom", state.room.code);
      updateRoomPanel();
      showToast(
        `Room ${state.room.code} is ready. Share the code with your class.`,
      );
    } else if (action === "delete-quiz") {
      if (!confirm("Delete this quiz? This action cannot be undone.")) return;
      await api(`/api/teacher/quizzes/${button.dataset.id}`, {
        method: "DELETE",
      });
      await loadTeacher();
      showToast("Quiz deleted.");
    } else if (action === "start-room") {
      state.room = await api(
        `/api/teacher/rooms/${button.dataset.code}/start`,
        { method: "POST" },
      );
      updateRoomPanel();
    } else if (action === "finish-room") {
      state.room = await api(
        `/api/teacher/rooms/${button.dataset.code}/finish`,
        { method: "POST" },
      );
      updateRoomPanel();
    } else if (action === "copy-room-code") {
      const code = button.dataset.code;
      try {
        await navigator.clipboard.writeText(code);
      } catch {
        const temporaryInput = document.createElement("textarea");
        temporaryInput.value = code;
        temporaryInput.style.position = "fixed";
        temporaryInput.style.opacity = "0";
        document.body.append(temporaryInput);
        temporaryInput.select();
        document.execCommand("copy");
        temporaryInput.remove();
      }
      showToast("Room code copied. Share it with your students.");
    } else if (action === "fullscreen") {
      await document.documentElement.requestFullscreen?.();
      showToast("Full-screen mode is on.");
    } else if (action === "answer") {
      await submitStudentAnswer(button.dataset.question, Number(button.dataset.option));
    }
  } catch (error) {
    showToast(error.message);
    if (
      action === "create-room" ||
      action === "start-room" ||
      action === "finish-room"
    )
      await loadTeacher();
  }
});

document.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  try {
    if (form.id === "multiple-answer-form") {
      const selected = [...form.querySelectorAll("input[name=option]:checked")]
        .map((input) => Number(input.value));
      if (!selected.length) return showToast("Select at least one answer.");
      await submitStudentAnswer(form.dataset.questionId, selected);
    } else if (form.id === "text-answer-form") {
      await submitStudentAnswer(form.dataset.questionId, form.elements.answer.value);
    } else if (form.id === "matching-answer-form") {
      const matches = Object.fromEntries(
        [...form.querySelectorAll(".matching-select")].map((select) => [
          select.dataset.leftKey,
          Number(select.value),
        ]),
      );
      await submitStudentAnswer(form.dataset.questionId, matches);
    } else if (form.id === "join-form") {
      const body = Object.fromEntries(new FormData(form));
      const room = await api("/api/student/join", { method: "POST", body });
      loadStudentRoom(room, room.student);
    } else if (form.id === "login-form") {
      const result = await api("/api/auth/login", {
        method: "POST",
        body: Object.fromEntries(new FormData(form)),
      });
      state.teacherToken = result.token;
      sessionStorage.setItem("teacherToken", result.token);
      await loadTeacher();
    } else if (form.id === "register-form") {
      const result = await api("/api/auth/register", {
        method: "POST",
        body: Object.fromEntries(new FormData(form)),
      });
      state.teacherToken = result.token;
      sessionStorage.setItem("teacherToken", result.token);
      state.authMode = "login";
      await loadTeacher();
    } else if (form.id === "quiz-form") {
      const questions = [...document.querySelectorAll(".question-card")].map((card) => {
        const type = card.querySelector(".question-type").value;
        const question = {
          type,
          text: card.querySelector(".question-text").value.trim(),
        };
        if (type === "text") {
          question.expected_answer = card.querySelector(".expected-answer").value.trim();
        } else if (type === "matching") {
          question.pairs = [...card.querySelectorAll(".pair-edit-row")].map((row) => ({
            left: row.querySelector(".match-left").value.trim(),
            right: row.querySelector(".match-right").value.trim(),
          }));
        } else {
          question.options = [...card.querySelectorAll(".option-text")].map((input) => input.value.trim());
          if (type === "multiple_choice") {
            question.correct_indices = [...card.querySelectorAll("input[type=checkbox]:checked")]
              .map((input) => Number(input.value));
          } else {
            question.correct_index = Number(card.querySelector("input[type=radio]:checked")?.value ?? -1);
          }
        }
        return question;
      });
      const quiz = await api(
        state.editingQuizId
          ? `/api/teacher/quizzes/${state.editingQuizId}`
          : "/api/teacher/quizzes",
        {
        method: state.editingQuizId ? "PUT" : "POST",
        body: {
          title: form.elements.title.value,
          duration_minutes: Number(form.elements.duration.value),
          questions,
        },
      });
      state.editingQuizId = null;
      showToast(`Quiz "${quiz.title}" ${quiz.created_at ? "saved" : "imported"}.`);
      await loadTeacher();
    }
  } catch (error) {
    const target =
      form.querySelector(".form-error") ||
      document.querySelector("#builder-error");
    if (target) target.textContent = error.message;
    else showToast(error.message);
  }
});

document.body.dataset.theme = localStorage.getItem("quizroomTheme") || "dark";
const savedAttempt = JSON.parse(
  localStorage.getItem("quizroomAttempt") || "null",
);
if (state.teacherToken) loadTeacher();
else renderHome();
