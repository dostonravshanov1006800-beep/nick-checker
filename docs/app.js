/* Nick Checker — единый UI для двух режимов:
   LOCAL  — FastAPI + SSE (запуск на своём компьютере/сервере)
   BOT   — мини-апп внутри бота: состояние читается из data-ветки GitHub,
           задания отправляются боту (sendData), ответы приходят в чат.
   Никаких фейковых результатов: всё, что показывается, пришло от реального движка. */
"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const STATUS = {
  free: { cls: "free", dot: "var(--free)", label: "свободен", emoji: "🟢" },
  taken: { cls: "taken", dot: "var(--taken)", label: "занят", emoji: "🔴" },
  unavailable: { cls: "unavail", dot: "var(--unavail)", label: "недоступен", emoji: "🟡" },
  error: { cls: "err", dot: "var(--err)", label: "ошибка", emoji: "⚪" },
};
const STATUS_RU = { free: "свободен", taken: "занят", unavailable: "недоступен", error: "ошибка" };

let MODE = null;            // 'local' | 'bot'
let tg = null;
let onlyFree = false;
let favSet = new Set();
let es = null, pollTimer = null;

/* ---------- утилиты ---------- */
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2200);
}
function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function haptic(kind) { try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred(kind || "light"); } catch (e) {} }
async function copyText(s) {
  try { await navigator.clipboard.writeText(s); toast("Скопировано: " + s); }
  catch (e) {
    const i = document.createElement("input"); i.value = s; document.body.appendChild(i);
    i.select(); document.execCommand("copy"); i.remove(); toast("Скопировано: " + s);
  }
}
function fmtEta(sec) {
  if (!sec) return "";
  if (sec < 60) return `осталось ~${sec} сек`;
  if (sec < 3600) return `осталось ~${Math.round(sec / 60)} мин`;
  return `осталось ~${(sec / 3600).toFixed(1)} ч`;
}
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleDateString("ru") + " " + d.toLocaleTimeString("ru", { hour: "2-digit", minute: "2-digit" });
}

/* ---------- карточка результата ---------- */
function resCard(r, opts) {
  const meta = STATUS[r.status] || STATUS.error;
  if (onlyFree && r.status !== "free" && !opts.force) return "";
  const owner = r.owner && r.owner.name ? `<div class="own">${esc(r.owner.type)}${r.owner.name ? " · " + esc(r.owner.name) : ""}</div>` : "";
  const err = r.error ? `<div class="err-t">${esc(r.error)}</div>` : "";
  const star = favSet.has(r.username);
  const cached = r.from_cache ? `<div class="cached">из кэша, ${Math.max(1, Math.round((Date.now() / 1000 - r.checked_at) / 60))} мин назад</div>` : "";
  const acts = MODE === "local"
    ? `<div class="act"><button class="iconb" data-copy="${esc(r.username)}">⧉</button><button class="iconb star ${star ? "on" : ""}" data-star="${esc(r.username)}">★</button></div>`
    : `<div class="act"><button class="iconb" data-copy="${esc(r.username)}">⧉</button></div>`;
  const badge = `<span class="st-badge" style="color:${meta.dot}">${meta.emoji} ${meta.label}</span>`;
  return `<div class="res"><span class="dot" style="background:${meta.dot}"></span>
    <div class="grow"><div class="u">${esc(r.username)}</div><div style="font-size:11.5px;font-weight:700;margin-top:2px">${badge}</div>${owner}${err}${cached}</div>${acts}</div>`;
}

/* ---------- задание (прогресс) ---------- */
const JOB_ST = { queued: "в очереди", running: "идёт проверка", paused: "пауза", flood: "ожидание Telegram", stopped: "остановлена", done: "готово ✓", idle: "" };
function renderJob(j) {
  const box = $("#job");
  if (!j || !j.status || j.status === "idle") { box.hidden = true; $("#feed-filter").hidden = false; return; }
  box.hidden = false;
  $("#job-label").textContent = j.label || "Проверка";
  $("#job-status").textContent = JOB_ST[j.status] || j.status;
  const pct = j.total ? Math.min(100, Math.round(j.done / j.total * 100)) : (j.status === "done" ? 100 : 0);
  $("#job-bar").style.width = pct + "%";
  $("#cnt-done").textContent = j.done + (j.total ? "/" + j.total : "");
  $("#cnt-free").textContent = j.counts.free; $("#cnt-taken").textContent = j.counts.taken;
  $("#cnt-unavail").textContent = j.counts.unavailable; $("#cnt-err").textContent = j.counts.error;
  const fl = $("#job-flood");
  if (j.status === "flood" || (j.note || "").includes("подождать")) { fl.hidden = false; fl.textContent = "⏳ " + (j.note || "Telegram просит подождать") + (j.flood_wait_left ? ` — ${j.flood_wait_left} сек` : ""); }
  else fl.hidden = true;
  $("#job-eta").textContent = j.note && j.status !== "flood" ? j.note : fmtEta(j.eta_sec);
  $("#btn-pause").textContent = j.status === "paused" ? "▶ Продолжить" : "⏸ Пауза";
  $("#jobctrl").style.display = (j.status === "running" || j.status === "paused" || j.status === "queued") && MODE === "local" ? "flex" : "none";
}

function feedItem(r) {
  const feed = $("#feed");
  if (onlyFree && r.status !== "free") return;
  const div = document.createElement("div");
  div.innerHTML = resCard(r, { force: true });
  feed.prepend(div.firstElementChild);
  while (feed.children.length > 120) feed.lastElementChild.remove();
}

/* ---------- режим LOCAL (FastAPI + SSE) ---------- */
const api = async (path, body, method) => {
  const res = await fetch(path, {
    method: method || (body ? "POST" : "GET"),
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) { location.reload(); throw new Error("нужен вход"); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || "ошибка запроса");
  return res.json();
};

async function startSSE() {
  if (es) es.close();
  es = new EventSource("/api/events");
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === "job") { renderJob(ev.job); if (ev.job.status === "done") loadHistory(); }
    if (ev.type === "result") feedItem(ev.result);
  };
}

/* ---------- режим BOT (мини-апп, данные из data-ветки) ---------- */
const RAW = "https://raw.githubusercontent.com/dostonravshanov1006800-beep/nick-checker/data";
let meId = null;
function botUrl(file) { return `${RAW}/users/${meId}/${file}.json?t=${Date.now()}`; }
const rawGet = async (u) => { try { return await (await fetch(u, { cache: "no-store" })).json(); } catch (e) { return null; } }

async function botPoll() {
  if (document.hidden) return;
  const j = await rawGet(botUrl("state"));
  if (j) renderJob(j);
  const favs = await rawGet(botUrl("favorites"));
  if (favs && Array.isArray(favs)) { favSet = new Set(favs.map((f) => f.username)); renderFavs(favs); }
}

async function botLoadFeed() {
  const j = await rawGet(botUrl("state"));
  if (j) { renderJob(j); (j.last_results || []).slice().reverse().forEach((r) => feedItem(r)); }
}

/* Канал апп -> бот: deep link t.me/<bot>?start=j_<base64url(JSON)>.
   Работает из синей меню-кнопки (sendData оттуда не доставляется). Пользователь
   жмёт «Старт» в чате, бот запускает задание, прогресс и результаты идут в приложение. */
const BOT_USERNAME = "adsmining_bot";
function b64url(obj) {
  const bytes = new TextEncoder().encode(JSON.stringify(obj));
  let bin = ""; bytes.forEach((x) => (bin += String.fromCharCode(x)));
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function compactPayload(p) {
  // deep link ограничен 64 символами payload, поэтому режем длинное и шлём компактно
  const c = { t: p.type, m: p.mode };
  if (p.username) c.u = p.username;
  if (p.base) c.b = p.base;
  if (p.pattern) c.p = p.pattern;
  if (p.length) c.l = p.length;
  if (p.style) c.s = p.style;
  if (p.limit) c.n = p.limit;
  if (p.text) c.x = p.text;
  if (p.random) c.r = 1;
  return c;
}
function sendData(payload) {
  // 1) из клавиатурной кнопки (редкий случай) - родная доставка
  // 2) иначе - deep link, открывает чат с ботом, одно нажатие «Старт»
  const link = `https://t.me/${BOT_USERNAME}?start=j_${b64url(compactPayload(payload))}`;
  if (link.length > 2000) { toast("Слишком длинный список: уменьшите до ~300 ников или отправьте файлом в чат"); return; }
  haptic("medium");
  watchForJob();
  if (tg && tg.openTelegramLink) tg.openTelegramLink(link); else window.open(link, "_blank");
}
let watchTimer = null;
function watchForJob() {
  // после возврата из чата быстро подтягиваем прогресс
  clearInterval(watchTimer);
  let n = 0;
  watchTimer = setInterval(() => { botPoll(); if (++n > 200) clearInterval(watchTimer); }, 3000);
}

/* ---------- избранное ---------- */
async function renderFavs(list) {
  const feed = $("#fav-feed");
  if (!list || !list.length) { feed.innerHTML = `<div class="empty">Пока пусто. Отмечайте ★ ники, которые хотите занять.</div>`; return; }
  feed.innerHTML = list.map((f) => resCard({ username: f.username, status: f.last_status || "unknown" }, { force: true })
    || `<div class="res"><div class="grow"><div class="u">${esc(f.username)}</div></div></div>`).join("")
    || `<div class="empty">Пока пусто</div>`;
}

/* ---------- история ---------- */
async function loadHistory() {
  const search = $("#hist-search").value.trim();
  const status = $("#hist-chips .chip.active").dataset.s || "";
  if (MODE === "local") {
    const rows = await api(`/api/history?search=${encodeURIComponent(search)}&status=${status}`);
    renderHistory(rows);
  } else {
    const rows = (await rawGet(botUrl("history"))) || [];
    const f = rows.filter((r) => (!search || r.username.includes(search)) && (!status || r.status === status));
    renderHistory(f);
  }
}
function renderHistory(rows) {
  const feed = $("#hist-feed");
  if (!rows.length) { feed.innerHTML = `<div class="empty">Проверок пока не было</div>`; return; }
  feed.innerHTML = rows.slice(0, 400).map((r) => resCard({
    username: r.username, status: r.status, owner: r.owner_type ? { type: r.owner_type, name: r.owner_name } : null,
    error: r.error, from_cache: false,
  }, { force: true })).join("");
}
function exportCsv(rows, fname) {
  const csv = "username,status,owner,checked_at\n" + rows.map((r) =>
    `${r.username},${STATUS_RU[r.status] || r.status},${r.owner_type || ""},${fmtTime(r.checked_at)}`).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["\ufeff" + csv], { type: "text/csv" }));
  a.download = fname; a.click();
}

/* ---------- отправка заданий ---------- */
function readJobParams(kind) {
  const delay = (sel) => Math.max(0.5, parseFloat($(sel).value) || 1.5);
  if (kind === "list") return { mode: "list", text: $("#in-list").value, delay: delay("#in-delay-list"), limit: +$("#in-limit-list").value };
  if (kind === "pattern") return { mode: "pattern", pattern: $("#in-pattern").value.trim().toLowerCase(), delay: delay("#in-delay-pat"), limit: +$("#in-limit-pat").value, random: $("#in-rand").checked };
  if (kind === "gen") return { mode: "random", length: +$("#in-len").value, style: $("#in-style").value, delay: delay("#in-delay-gen"), limit: +$("#in-limit-gen").value };
  if (kind === "dict") return { mode: "dictionary", text: $("#in-dict").value, delay: delay("#in-delay-dict"), limit: +$("#in-limit-dict").value };
  if (kind === "auto") return { mode: "auto", base: $("#in-auto").value.trim().toLowerCase(), delay: delay("#in-delay-auto"), limit: +$("#in-limit-auto").value };
}
async function submitJob(kind) {
  haptic("medium");
  const p = readJobParams(kind);
  if (p.mode === "pattern" && !p.pattern) { toast("Введите шаблон, например ka??a"); return; }
  if (p.mode === "auto" && !p.base) { toast("Введите желаемый ник"); return; }
  if ((p.mode === "list" || p.mode === "dictionary") && !p.text.trim()) { toast("Список пуст"); return; }
  $("#feed").innerHTML = "";
  if (MODE === "local") {
    try {
      const j = await api("/api/jobs", p);
      renderJob(j); toast("Запущено");
    } catch (e) { toast(e.message); }
  } else {
    sendData({ type: "job", ...p });
    toast("Нажмите «Старт» в чате с ботом: результаты появятся здесь");
  }
}
async function submitOne() {
  haptic("light");
  const u = $("#in-one").value.trim();
  if (!u) { toast("Введите ник"); return; }
  if (MODE === "local") {
    $("#in-one").value = "";
    const r = await api("/api/check", { username: u });
    feedItem(r); loadHistory();
  } else {
    sendData({ type: "single", username: u });
    toast("Нажмите «Старт» в чате: результат появится здесь");
  }
}
async function submitFavCheck() {
  haptic("medium");
  if (MODE === "local") {
    const favs = await api("/api/favorites");
    if (!favs.length) { toast("Избранное пустое"); return; }
    await submitJobByText(favs.map((f) => f.username), "избранное · " + favs.length);
  } else {
    sendData({ type: "job", mode: "favorites" });
    toast("Нажмите «Старт» в чате: результаты появятся здесь");
  }
}
async function submitJobByText(items, label) {
  $("#feed").innerHTML = "";
  const j = await api("/api/jobs", { mode: "list", text: items.join(","), delay: 1.5, limit: 5000 });
  renderJob(j); toast("Запущено: " + label);
}

/* ---------- файлы .txt/.csv ---------- */
function attachFileSlot(inputId, textareaId) {
  const inp = document.createElement("input");
  inp.type = "file"; inp.accept = ".txt,.csv,text/plain,text/csv"; inp.hidden = true;
  inp.onchange = () => {
    const f = inp.files[0]; if (!f) return;
    const rd = new FileReader();
    rd.onload = () => { $(textareaId).value = rd.result; toast(`Загружено: ${f.name}`); };
    rd.readAsText(f);
  };
  const a = document.createElement("a");
  a.href = "#"; a.textContent = "выбрать файл";
  a.onclick = (e) => { e.preventDefault(); inp.click(); };
  $("#file-slot").append(inp, a);
}

/* ---------- инициализация ---------- */
function detectUser() {
  try { if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) return tg.initDataUnsafe.user; } catch (e) {}
  try { const q = new URLSearchParams(location.search).get("uid"); if (q && /^\d+$/.test(q)) return { id: +q }; } catch (e) {}
  try { const c = JSON.parse(localStorage.getItem("nc_me") || "null"); if (c && c.id) return c; } catch (e) {}
  return null;
}

async function init() {
  tg = window.Telegram && window.Telegram.WebApp;
  try { tg && tg.ready && tg.ready(); tg && tg.expand && tg.expand(); } catch (e) {}
  const me = detectUser(); meId = me ? me.id : null;
  if (me && me.id) { try { localStorage.setItem("nc_me", JSON.stringify({ id: me.id })); } catch (e) {} }

  let local = false;
  try { const p = await (await fetch("/api/ping", { cache: "no-store" })).json(); local = p && p.mode === "local"; } catch (e) {}
  MODE = local ? "local" : "bot";
  $("#exp-all").style.display = $("#exp-free").style.display = MODE === "local" ? "" : "flex";

  if (MODE === "bot") {
    $("#bot-note").hidden = false;
    $("#bot-note").textContent = "Запуск: кнопка → чат с ботом → «Старт». Прогресс и результаты обновляются здесь автоматически.";
    $("#jobctrl").style.display = "none";
    botLoadFeed();
    pollTimer = setInterval(botPoll, 4000);
  } else {
    startSSE();
    loadHistory();
    const favs = await api("/api/favorites");
    favSet = new Set(favs.map((f) => f.username)); renderFavs(favs);
  }

  /* табы */
  $$("#tabbar button").forEach((b) => b.onclick = () => {
    haptic("selection");
    $$("#tabbar button").forEach((x) => x.classList.toggle("active", x === b));
    ["check", "history", "fav", "info"].forEach((p) => $("#page-" + p).hidden = b.dataset.tab !== p);
    if (b.dataset.tab === "history") loadHistory();
    if (b.dataset.tab === "fav" && MODE === "bot") renderFavs(Array.from(favSet, (u) => ({ username: u })));
  });

  /* режимы проверки */
  $$("#mode-seg button").forEach((b) => b.onclick = () => {
    haptic("selection");
    $$("#mode-seg button").forEach((x) => x.classList.toggle("active", x === b));
    ["one", "list", "pattern", "gen", "auto", "dict"].forEach((m) => $("#form-" + m).hidden = m !== b.dataset.m);
  });

  /* кнопки */
  $("#btn-one").onclick = submitOne;
  $("#in-one").addEventListener("keydown", (e) => { if (e.key === "Enter") submitOne(); });
  $("#btn-list").onclick = () => submitJob("list");
  $("#btn-pattern").onclick = () => submitJob("pattern");
  $("#btn-gen").onclick = () => submitJob("gen");
  $("#btn-dict").onclick = () => submitJob("dict");
  $("#btn-auto").onclick = () => submitJob("auto");
  $("#in-auto").addEventListener("keydown", (e) => { if (e.key === "Enter") submitJob("auto"); });
  $("#btn-check-fav").onclick = submitFavCheck;
  attachFileSlot("#file-slot", "#in-list");

  $("#btn-pause").onclick = async () => { haptic(); const st = $("#btn-pause").textContent.includes("Продолжить") ? "resume" : "pause"; await api("/api/job/control", { action: st }); };
  $("#btn-stop").onclick = async () => { haptic("heavy"); await api("/api/job/control", { action: "stop" }); };

  $("#only-free").onchange = (e) => { onlyFree = e.target.checked; };
  $("#hist-search").addEventListener("input", () => { if (MODE === "local") loadHistory(); });
  $$("#hist-chips .chip[data-s]").forEach((c) => c.onclick = () => {
    $$("#hist-chips .chip[data-s]").forEach((x) => x.classList.toggle("active", x === c));
    loadHistory();
  });
  $("#hist-sort").onclick = () => loadHistory();

  /* экспорт */
  $("#exp-all").onclick = async (e) => { e.preventDefault(); const rows = await api("/api/history?"); exportCsv(rows, "history.csv"); };
  $("#exp-free").onclick = async (e) => { e.preventDefault(); const rows = (await api("/api/history?")).filter((r) => r.status === "free"); exportCsv(rows, "free.csv"); };

  /* действия в карточках: копия и звезда */
  document.addEventListener("click", (e) => {
    const cp = e.target.closest("[data-copy]"); if (cp) { haptic(); copyText(cp.dataset.copy); }
    const st = e.target.closest("[data-star]");
    if (st && MODE === "local") {
      haptic(); const u = st.dataset.star;
      api("/api/favorite", { username: u }).then(async (r) => {
        const favs = await api("/api/favorites"); favSet = new Set(favs.map((f) => f.username));
        renderFavs(favs); st.classList.toggle("on", r.added);
      });
    }
  });
}
init();
