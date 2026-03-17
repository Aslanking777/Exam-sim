/* global Telegram */

const TG = window.Telegram?.WebApp;
if (TG) {
  TG.expand();
  TG.ready();
}

const $ = (id) => document.getElementById(id);

const screens = {
  loading: $("screenLoading"),
  preReview: $("screenPreReview"),
  test: $("screenTest"),
  break: $("screenBreak"),
  done: $("screenDone"),
};

function showScreen(name) {
  Object.values(screens).forEach((el) => el.classList.remove("is-active"));
  screens[name].classList.add("is-active");
}

function qs(name) {
  return new URLSearchParams(window.location.search).get(name);
}

function fmtTime(sec) {
  const s = Math.max(0, Math.floor(sec));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

const FLOW = [
  { key: "EBRW", module: 1, questions: 27, seconds: 32 * 60 },
  { key: "EBRW", module: 2, questions: 27, seconds: 32 * 60 },
  { key: "BREAK", module: 0, questions: 0, seconds: 15 * 60 },
  { key: "MATH", module: 1, questions: 22, seconds: 35 * 60 },
  { key: "MATH", module: 2, questions: 22, seconds: 35 * 60 },
];

let apiBase = (qs("api") || "").replace(/\/+$/, "");
let testId = Number(qs("test_id") || "0");

const state = {
  test: null,
  stepIdx: 0, // index into FLOW
  moduleLocked: {}, // stepIdx -> true
  moduleSubmittedAt: {}, // stepIdx -> timestamp
  qIdxInModule: 0,
  answers: {}, // questionId -> "A"|"B"|"C"|"D"
  perQuestionSeconds: {}, // questionId -> seconds
  qEnterTs: null,
  timer: {
    running: false,
    remaining: 0,
    interval: null,
  },
  highlightMode: false,
};

function currentStep() {
  return FLOW[state.stepIdx];
}

function isMathStep(step) {
  return step.key === "MATH";
}

function isEbrwStep(step) {
  return step.key === "EBRW";
}

function isBreakStep(step) {
  return step.key === "BREAK";
}

function updateTopbar() {
  const step = currentStep();
  const pill = $("sectionPill");
  if (isBreakStep(step)) pill.textContent = "BREAK";
  else pill.textContent = `${step.key} • Module ${step.module}`;
}

function getModuleQuestions() {
  const step = currentStep();
  if (isBreakStep(step)) return [];
  const start = FLOW.slice(0, state.stepIdx).reduce((acc, s) => acc + (s.questions || 0), 0);
  return state.test.questions.slice(start, start + step.questions);
}

function activeQuestion() {
  return getModuleQuestions()[state.qIdxInModule];
}

function logQuestionExit() {
  const q = activeQuestion();
  if (!q || state.qEnterTs == null) return;
  const delta = Math.max(0, (Date.now() - state.qEnterTs) / 1000);
  state.perQuestionSeconds[q.id] = (state.perQuestionSeconds[q.id] || 0) + delta;
  state.qEnterTs = Date.now();
}

function renderBridge() {
  const bridge = $("bridge");
  bridge.innerHTML = "";
  const qsMod = getModuleQuestions();
  qsMod.forEach((q, idx) => {
    const btn = document.createElement("button");
    btn.className = "bridgeBtn";
    btn.type = "button";
    btn.textContent = String(idx + 1);
    const dot = document.createElement("div");
    dot.className = "dot";
    btn.appendChild(dot);
    if (idx === state.qIdxInModule) btn.classList.add("is-active");
    if (state.answers[q.id]) btn.classList.add("is-answered");
    if (state.moduleLocked[state.stepIdx]) btn.classList.add("is-locked");
    btn.addEventListener("click", () => {
      if (state.moduleLocked[state.stepIdx]) return;
      logQuestionExit();
      state.qIdxInModule = idx;
      renderQuestion();
      renderBridge();
    });
    bridge.appendChild(btn);
  });
}

function renderTools() {
  const step = currentStep();
  const btnDesmos = $("btnDesmos");
  const btnHighlight = $("btnHighlight");
  const desmosPanel = $("desmosPanel");

  btnDesmos.disabled = !isMathStep(step);
  btnDesmos.style.display = isMathStep(step) ? "inline-flex" : "none";
  desmosPanel.hidden = true;

  btnHighlight.disabled = !isEbrwStep(step);
  btnHighlight.style.display = isEbrwStep(step) ? "inline-flex" : "none";
  btnHighlight.classList.toggle("is-on", state.highlightMode);
}

function renderQuestion() {
  const step = currentStep();
  updateTopbar();
  renderTools();

  const q = activeQuestion();
  $("qMeta").textContent = isBreakStep(step)
    ? "Break"
    : `${step.key} • Module ${step.module} • Question ${state.qIdxInModule + 1} of ${step.questions}`;

  $("qStem").innerHTML = "";
  $("qOptions").innerHTML = "";
  $("lockHint").textContent = state.moduleLocked[state.stepIdx]
    ? "Module locked. Moving on…"
    : "You can review within this module until time expires or you submit the module.";

  if (!q) return;

  // Render stem as text but allow highlighting spans
  const stem = document.createElement("div");
  stem.textContent = q.stem;
  $("qStem").appendChild(stem);

  const selected = state.answers[q.id] || null;
  q.options.forEach((optText, i) => {
    const key = ["A", "B", "C", "D"][i] || String(i + 1);
    const el = document.createElement("div");
    el.className = "opt";
    if (selected === key) el.classList.add("is-selected");
    el.addEventListener("click", () => {
      if (state.moduleLocked[state.stepIdx]) return;
      state.answers[q.id] = key;
      renderQuestion();
      renderBridge();
    });
    const k = document.createElement("div");
    k.className = "opt__key";
    k.textContent = key;
    const t = document.createElement("div");
    t.className = "opt__text";
    t.textContent = optText;
    el.appendChild(k);
    el.appendChild(t);
    $("qOptions").appendChild(el);
  });
}

function startTimer(seconds, onDone) {
  clearInterval(state.timer.interval);
  state.timer.remaining = seconds;
  state.timer.running = true;
  $("timer").textContent = fmtTime(state.timer.remaining);
  state.timer.interval = setInterval(() => {
    state.timer.remaining -= 1;
    $("timer").textContent = fmtTime(state.timer.remaining);
    if (state.timer.remaining <= 0) {
      clearInterval(state.timer.interval);
      state.timer.running = false;
      onDone?.();
    }
  }, 1000);
}

function lockModuleAndAdvance(reason) {
  state.moduleLocked[state.stepIdx] = true;
  state.moduleSubmittedAt[state.stepIdx] = Date.now();
  $("lockHint").textContent = `Module locked (${reason}).`;
  setTimeout(() => advanceStep(), 450);
}

function advanceStep() {
  logQuestionExit();
  state.stepIdx += 1;
  state.qIdxInModule = 0;
  state.qEnterTs = Date.now();

  if (state.stepIdx >= FLOW.length) {
    finishTest();
    return;
  }

  const step = currentStep();
  if (isBreakStep(step)) {
    showBreak();
  } else {
    showTest();
  }
}

function showBreak() {
  showScreen("break");
  $("breakTimer").textContent = "15:00";
  $("timer").textContent = "15:00";
  updateTopbar();
  startTimer(15 * 60, () => advanceStep());
}

function showTest() {
  showScreen("test");
  const step = currentStep();
  updateTopbar();
  renderBridge();
  renderQuestion();
  startTimer(step.seconds, () => lockModuleAndAdvance("time expired"));
}

function computeScore() {
  let score = 0;
  state.test.questions.forEach((q) => {
    if (state.answers[q.id] && state.answers[q.id] === q.correct_answer) score += 1;
  });
  return score;
}

function buildReport() {
  const per_question = Object.entries(state.perQuestionSeconds).map(([qid, sec]) => ({
    question_id: Number(qid),
    seconds_spent: Math.round(sec),
  }));
  // Ensure all questions have an entry (even if 0)
  state.test.questions.forEach((q) => {
    if (!(q.id in state.perQuestionSeconds)) per_question.push({ question_id: q.id, seconds_spent: 0 });
  });

  return {
    test_id: state.test.id,
    total_score: computeScore(),
    per_question,
    answers: state.answers,
    finished_at: new Date().toISOString(),
  };
}

function finishTest() {
  clearInterval(state.timer.interval);
  showScreen("done");
  $("timer").textContent = "00:00";
  const report = buildReport();
  $("doneSummary").textContent = `Score: ${report.total_score}. Tap “Send report” to return to Telegram.`;
  $("btnSendNow").onclick = () => {
    if (!TG) {
      alert("Telegram WebApp not detected. Copying report to clipboard.");
      navigator.clipboard?.writeText(JSON.stringify(report));
      return;
    }
    TG.sendData(JSON.stringify(report));
    TG.close();
  };
}

function buildPreReviewList(flagged) {
  const wrap = $("reviewList");
  wrap.innerHTML = "";
  flagged.forEach((q) => {
    const item = document.createElement("div");
    item.className = "reviewItem";

    const head = document.createElement("div");
    head.className = "reviewHead";
    const left = document.createElement("div");
    left.innerHTML = `<strong>Question ${q.idx}</strong>`;
    const tag = document.createElement("div");
    tag.className = "tag tag--warn";
    tag.textContent = q.status === "needs_correction" ? "Needs correction" : `Low confidence (${q.confidence_score})`;
    head.appendChild(left);
    head.appendChild(tag);

    const stem = document.createElement("textarea");
    stem.value = q.stem;
    stem.addEventListener("input", () => {
      q.stem = stem.value;
    });

    const opts = document.createElement("div");
    opts.className = "grid2";
    const inputs = [];
    (q.options || []).slice(0, 4).forEach((t, i) => {
      const inp = document.createElement("input");
      inp.value = t;
      inp.addEventListener("input", () => {
        q.options[i] = inp.value;
      });
      inputs.push(inp);
      opts.appendChild(inp);
    });

    const note = document.createElement("div");
    note.className = "small";
    note.textContent = "Edits here are local to this Web App session.";

    item.appendChild(head);
    item.appendChild(stem);
    item.appendChild(opts);
    item.appendChild(note);
    wrap.appendChild(item);
  });
}

function attachHandlers() {
  $("btnPrev").addEventListener("click", () => {
    if (state.moduleLocked[state.stepIdx]) return;
    if (state.qIdxInModule <= 0) return;
    logQuestionExit();
    state.qIdxInModule -= 1;
    renderQuestion();
    renderBridge();
  });

  $("btnNext").addEventListener("click", () => {
    if (state.moduleLocked[state.stepIdx]) return;
    const step = currentStep();
    if (state.qIdxInModule >= step.questions - 1) return;
    logQuestionExit();
    state.qIdxInModule += 1;
    renderQuestion();
    renderBridge();
  });

  $("btnSubmitModule").addEventListener("click", () => {
    if (state.moduleLocked[state.stepIdx]) return;
    lockModuleAndAdvance("submitted");
  });

  $("btnSkipBreak").addEventListener("click", () => {
    clearInterval(state.timer.interval);
    advanceStep();
  });

  $("btnCloseDesmos").addEventListener("click", () => {
    $("desmosPanel").hidden = true;
  });

  $("btnDesmos").addEventListener("click", () => {
    $("desmosPanel").hidden = false;
  });

  $("btnHighlight").addEventListener("click", () => {
    state.highlightMode = !state.highlightMode;
    renderTools();
  });

  document.addEventListener("mouseup", () => {
    const step = currentStep();
    if (!isEbrwStep(step) || !state.highlightMode) return;
    if (state.moduleLocked[state.stepIdx]) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    if (range.collapsed) return;

    const stemEl = $("qStem");
    if (!stemEl.contains(range.commonAncestorContainer)) return;

    try {
      const span = document.createElement("span");
      span.className = "hl";
      range.surroundContents(span);
      sel.removeAllRanges();
    } catch {
      // If selection crosses nodes, ignore (keep UX predictable)
    }
  });

  $("btnStartTest").addEventListener("click", () => {
    state.stepIdx = 0;
    state.qIdxInModule = 0;
    state.qEnterTs = Date.now();
    showTest();
  });
}

async function loadTest() {
  if (!testId) throw new Error("Missing test_id in URL.");
  if (!apiBase) throw new Error("Missing api= in URL (public backend base URL).");

  const res = await fetch(`${apiBase}/api/test/${testId}`, { method: "GET" });
  if (!res.ok) throw new Error(`API error (${res.status})`);
  return await res.json();
}

function setMeta() {
  const title = state.test.title ? `• ${state.test.title}` : "";
  $("testMeta").textContent = `Test #${state.test.id} ${title}`;
}

async function boot() {
  attachHandlers();
  showScreen("loading");

  try {
    const t = await loadTest();
    state.test = t;
    setMeta();
    const flagged = t.questions.filter((q) => q.status === "needs_correction" || (q.confidence_score || 0) < 60);
    if (flagged.length) {
      buildPreReviewList(flagged);
      showScreen("preReview");
      $("timer").textContent = "--:--";
      $("sectionPill").textContent = "PRE-REVIEW";
    } else {
      state.qEnterTs = Date.now();
      showTest();
    }
  } catch (e) {
    $("loadingHint").textContent =
      `Could not load test.\n` +
      `Ensure the URL has both test_id and api, e.g. ?test_id=1&api=https://your-backend\n\n` +
      `${String(e)}`;
  }
}

boot();

