// دليل — واجهة المحادثة والفهرس والسند.
//
// عقد الخادم لم يتغيّر:
//   POST /chat  { history, service_id, clarify_count }
//               → { format: answer|card|clarify|prose|refusal, reply|answer,
//                   service_name, authority, confidence, sources[], answer_time }
//   #directory-data  ← {{ directory | tojson }}
//
// ما تغيّر في الواجهة فقط: الفهرس صار شريطاً دائماً، والسند شريطاً ثانياً،
// وكلاهما قابل للطي، ولا عدّاد حصة على الشاشة.

const shellEl     = document.getElementById("shell");
const messagesEl  = document.getElementById("messages");
const threadEl    = document.getElementById("thread");
const formEl      = document.getElementById("composer");
const inputEl     = document.getElementById("input");
const sendBtn     = document.getElementById("send");
const themeBtn    = document.getElementById("theme-toggle");
const srcBodyEl   = document.getElementById("src-body");
const srcNoteEl   = document.getElementById("src-note");
const foldSrcEl   = document.getElementById("fold-count-src");
const foldIdxEl   = document.getElementById("fold-count-index");
const scrimEl     = document.querySelector(".rail__scrim");

const history = [];
let clarifyCount = 0;
let busy = false;

const CONF_LABEL   = { high: "معلومات مؤكدة", partial: "معلومات جزئية" };
const NETWORK_ERROR = "تعذّر الاتصال بالخادم. تأكد من تشغيله ثم أعد المحاولة.";

// ترميز القطاعات: الفهرس لا يحمل حقل قطاع، فتُوزَّع الألوان بالترتيب
// حتى يبقى للجهة لون ثابت يتعرّف عليه المستخدم.
const SECTORS = ["#0f7b78", "#8a5410", "#33608f", "#6d4b8f", "#9e3b32"];

const AR_DIGITS = ["٠", "١", "٢", "٣", "٤", "٥", "٦", "٧", "٨", "٩"];
const toArabicDigits = (value) =>
  String(value).replace(/\d/g, (d) => AR_DIGITS[Number(d)]);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(paths, size) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.6");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("aria-hidden", "true");
  if (size) { svg.setAttribute("width", size); svg.setAttribute("height", size); }
  paths.forEach((d) => {
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", d);
    svg.appendChild(p);
  });
  return svg;
}

function scrollToEnd() {
  threadEl.scrollTop = threadEl.scrollHeight;
}

function bubble(role) {
  const node = el("div", "msg " + (role === "user" ? "msg--user" : "msg--bot"));
  messagesEl.appendChild(node);
  scrollToEnd();
  return node;
}

/* ------------------------------------------------------------------ السمة */

function currentTheme() {
  const stamped = document.documentElement.getAttribute("data-theme");
  if (stamped === "light" || stamped === "dark") return stamped;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function labelTheme(theme) {
  themeBtn.setAttribute(
    "aria-label",
    theme === "dark" ? "التبديل إلى الوضع الفاتح" : "التبديل إلى الوضع الداكن"
  );
}

function setTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  try { localStorage.setItem("daleel-theme", theme); } catch (e) {}
  labelTheme(theme);
}

themeBtn.addEventListener("click", () => {
  setTheme(currentTheme() === "dark" ? "light" : "dark");
});
labelTheme(currentTheme());

/* --------------------------------------------------- طي الشريطين وفتحهما */
//
// سطح المكتب: الشريط يتقلّص إلى شريحة رأسية (index-folded / src-folded).
// الجوال: الشريط لوحة منزلقة فوق المحتوى (index-open / src-open).
// الحالة تُحفظ في localStorage حتى تبقى كما تركها المستخدم.

const isMobile = () => window.matchMedia("(max-width: 960px)").matches;

function syncFoldButtons() {
  document.querySelectorAll("[data-fold]").forEach((btn) => {
    const key = btn.dataset.fold;
    const folded = shellEl.classList.contains(`${key}-folded`);
    const inStrip = Boolean(btn.closest(".rail__strip"));
    btn.setAttribute("aria-expanded", folded ? "false" : "true");
    const name = key === "index" ? "الفهرس" : "السند";
    btn.setAttribute("aria-label", (inStrip ? "فتح " : "طي ") + name);
  });
}

function setFolded(key, folded) {
  shellEl.classList.toggle(`${key}-folded`, folded);
  try {
    localStorage.setItem(`daleel-rail-${key}`, folded ? "folded" : "open");
  } catch (e) {}
  syncFoldButtons();
}

function setMobilePanel(key) {
  const open = key && !shellEl.classList.contains(`${key}-open`);
  shellEl.classList.remove("index-open", "src-open");
  if (open) shellEl.classList.add(`${key}-open`);
  document.querySelectorAll("[data-mobile]").forEach((btn) => {
    const target = btn.dataset.mobile;
    const active = open ? target === key : target === "ask";
    btn.classList.toggle("is-active", active);
  });
}

document.querySelectorAll("[data-fold]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const key = btn.dataset.fold;
    if (isMobile()) setMobilePanel(key);
    else setFolded(key, !shellEl.classList.contains(`${key}-folded`));
  });
});

document.querySelectorAll("[data-mobile]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const target = btn.dataset.mobile;
    if (target === "ask") { setMobilePanel(null); inputEl.focus(); }
    else setMobilePanel(target);
  });
});

scrimEl.addEventListener("click", () => setMobilePanel(null));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setMobilePanel(null);
});

// استعادة ما حُفظ (الوسم على <html> كُتب في الرأس قبل أول رسم).
(function restoreRails() {
  const stamped = document.documentElement.getAttribute("data-rails") || "";
  stamped.split(/\s+/).filter(Boolean).forEach((cls) => shellEl.classList.add(cls));
  syncFoldButtons();
})();

/* -------------------------------------------------- تحويل نص الإجابة (markdown) */
// النموذج قد يُصدر "- عنصر" و"1. خطوة" و**عريض** فقط. كله يُبنى عُقَداً
// حقيقية، لا innerHTML: النص مُخرَج نموذج ولا يجوز أن يحقن وسماً.

function appendInline(parent, text) {
  String(text).split(/\*\*(.+?)\*\*/g).forEach((part, i) => {
    if (!part) return;
    if (i % 2 === 1) parent.appendChild(el("strong", null, part));
    else parent.appendChild(document.createTextNode(part));
  });
}

function renderMarkdown(text) {
  const wrap = el("div", "answer__body");
  let list = null;

  String(text || "").split("\n").forEach((raw) => {
    const line = raw.trim();
    if (!line) { list = null; return; }

    const bullet   = line.match(/^[-•*]\s+(.*)$/);
    const numbered = line.match(/^(\d+)[.)]\s+(.*)$/);

    if (bullet || numbered) {
      const wantTag = numbered ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== wantTag) {
        list = el(wantTag, "answer__list");
        wrap.appendChild(list);
      }
      const li = el("li");
      appendInline(li, bullet ? bullet[1] : numbered[2]);
      list.appendChild(li);
      return;
    }

    list = null;
    const p = el("p", "answer__p");
    appendInline(p, line);
    wrap.appendChild(p);
  });

  return wrap;
}

/* ------------------------------------------------------------ وسم المصدر */

function hostOf(url) {
  try { return new URL(url).hostname.toLowerCase(); } catch (e) { return ""; }
}

function isOfficial(url) {
  const host = hostOf(url);
  return host === "gov.om" || host.endsWith(".gov.om");
}

function sourceFlag(url) {
  if (!hostOf(url)) return null;
  const official = isOfficial(url);
  const badge = el(
    "span",
    "srcflag " + (official ? "srcflag--gov" : "srcflag--other"),
    official ? "مصدر حكومي رسمي" : "مصدر غير حكومي"
  );
  badge.title = hostOf(url);
  return badge;
}

function sourceList(data) {
  if (data.sources && data.sources.length) return data.sources;
  if (data.source_url || data.source_title) {
    return [{
      url: data.source_url, title: data.source_title, ref: data.source_title,
      service: data.service_name, authority: data.authority,
    }];
  }
  return [];
}

function sourceLabel(src) {
  return [src.authority, src.service].filter(Boolean).join(" — ")
    || src.title || src.ref || src.url || "مصدر";
}

/* ------------------------------------------------------- شريط السند (اليسار) */

function renderEvidence(data) {
  const list = sourceList(data);
  srcBodyEl.textContent = "";

  if (!list.length) {
    srcNoteEl.textContent = "لا سند";
    foldSrcEl.hidden = true;
    const empty = el("div", "srchint",
      "لا توجد بطاقة سند لهذا السؤال. ما لا سند له يُعرض تحفّظاً لا إجابة.");
    srcBodyEl.appendChild(empty);
    return;
  }

  srcNoteEl.textContent = list.length === 1 ? "مصدر واحد"
    : list.length === 2 ? "مصدران"
    : `${toArabicDigits(list.length)} مصادر`;
  foldSrcEl.hidden = false;
  foldSrcEl.textContent = toArabicDigits(list.length);

  list.forEach((src, index) => {
    const card = el("div", "srccard");

    const head = el("div", "srccard__head");
    head.appendChild(el("span", "srccard__num", toArabicDigits(index + 1)));
    head.appendChild(el("span", "srccard__auth", src.authority || sourceLabel(src)));
    if (src.url) head.appendChild(el("span", "srccard__kind", "الموقع الرسمي"));
    card.appendChild(head);

    const body = el("div", "srccard__body");
    if (src.service) body.appendChild(el("div", "srccard__svc", src.service));
    // المقتطف يُعرض حين يرسله الخادم (حقل quote أو snippet). وإلا فعنوان المصدر.
    const quote = src.quote || src.snippet;
    if (quote) body.appendChild(el("p", "srccard__quote", `«${quote}»`));
    else if (src.title) body.appendChild(el("p", "srccard__quote", src.title));

    const foot = el("div", "srccard__foot");
    const flag = sourceFlag(src.url);
    if (flag) foot.appendChild(flag);
    if (src.url) {
      const open = el("a", "srccard__open", "افتح المصدر");
      open.href = src.url;
      open.target = "_blank";
      open.rel = "noopener noreferrer";
      foot.appendChild(open);
    }
    if (foot.childNodes.length) body.appendChild(foot);

    card.appendChild(body);
    srcBodyEl.appendChild(card);
  });

  // حالة الإجابة: تُعرض التسمية التي يرسلها الخادم، لا نسبة مُختلقة.
  if (data.confidence && CONF_LABEL[data.confidence]) {
    const state = el("div", "srcstate");
    state.appendChild(el("div", "srcstate__label", CONF_LABEL[data.confidence]));
    state.appendChild(el("p", "srcstate__note",
      "الجهة المختصة هي المرجع النهائي لأي متطلب أو رسم."));
    srcBodyEl.appendChild(state);
  }
}

function resetEvidence(message) {
  srcBodyEl.textContent = "";
  srcNoteEl.textContent = message || "بانتظار سؤال";
  foldSrcEl.hidden = true;
  srcBodyEl.appendChild(el("div", "srchint",
    "تظهر هنا بطاقة لكل مصدر استندت إليه الإجابة، مع الجهة ونوع المصدر ورابطه."));
}

/* ----------------------------------------------------------- كتابة الإجابة */
// النص وصل كاملاً ومُتحقَّقاً منه؛ هذا كشف تدريجي لا بثّ حيّ. عرض رسم حكومي
// ثم سحبه أسوأ من انتظار قصير.

const TYPING_TARGET_MS = 1600;
const TYPING_MIN_CPS   = 45;

function textNodesOf(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  return nodes;
}

function typeOut(container, onDone) {
  const nodes = textNodesOf(container);
  const full  = nodes.map((n) => n.nodeValue);
  const total = full.reduce((sum, t) => sum + t.length, 0);

  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches || !total) {
    onDone();
    return () => {};
  }

  nodes.forEach((n) => (n.nodeValue = ""));
  container.classList.add("is-typing");

  const perFrame = Math.max(
    Math.ceil(total / (TYPING_TARGET_MS / 16)),
    Math.ceil(TYPING_MIN_CPS / 60)
  );
  let shown = 0, frame = null, done = false;

  function finish() {
    if (done) return;
    done = true;
    if (frame) cancelAnimationFrame(frame);
    clearTimeout(safety);
    nodes.forEach((n, i) => (n.nodeValue = full[i]));
    container.classList.remove("is-typing");
    onDone();
  }

  // requestAnimationFrame يتوقف في التبويب الخلفي؛ هذا المؤقّت يضمن وصول النص.
  const safety = setTimeout(finish, TYPING_TARGET_MS * 3);

  function step() {
    shown = Math.min(total, shown + perFrame);
    let left = shown;
    for (let i = 0; i < nodes.length; i++) {
      const text = full[i];
      if (left >= text.length) { nodes[i].nodeValue = text; left -= text.length; }
      else { nodes[i].nodeValue = text.slice(0, left); left = 0; }
    }
    scrollToEnd();
    if (shown < total) frame = requestAnimationFrame(step);
    else finish();
  }
  frame = requestAnimationFrame(step);
  return finish;
}

/* --------------------------------------------------------- عرض أنواع الردود */

const THINKING_PHASES = [
  { after: 0,    label: "يبحث في المصادر الرسمية…" },
  { after: 1400, label: "يصيغ الإجابة من المصادر…" },
  { after: 9000, label: "الإجابة تستغرق وقتاً أطول من المعتاد…" },
];

function setBusy(state) {
  busy = state;
  sendBtn.disabled = state;
  inputEl.disabled = state;
  messagesEl.setAttribute("aria-busy", state ? "true" : "false");
}

function showLoading() {
  const node = bubble("bot");
  const wrap = el("div", "loading");
  const dots = el("span", "typing");
  dots.append(el("span"), el("span"), el("span"));
  const label = el("span", "loading__label", THINKING_PHASES[0].label);
  wrap.append(dots, label);
  node.appendChild(wrap);
  scrollToEnd();

  const started = Date.now();
  const timers = THINKING_PHASES.slice(1).map((phase) =>
    setTimeout(() => {
      label.textContent = phase.label;
      if (phase.after >= 9000) wrap.classList.add("loading--slow");
    }, phase.after - (Date.now() - started))
  );
  node._stopThinking = () => timers.forEach(clearTimeout);
  return node;
}

function crumbs(data) {
  if (!data.authority && !data.service_name) return null;
  const row = el("div", "crumbs");
  if (data.authority) row.appendChild(el("span", null, data.authority));
  if (data.authority && data.service_name) {
    row.appendChild(el("span", "crumbs__sep", "◂"));
  }
  if (data.service_name) row.appendChild(el("span", "crumbs__now", data.service_name));
  return row;
}

function cardSources(data) {
  const list = sourceList(data);
  if (!list.length) return null;

  const box = el("div", "card__source");
  box.appendChild(el("div", "card__source-label",
    list.length > 1 ? "المصادر" : "المصدر"));

  list.forEach((src, index) => {
    const row = el("div", "card__source-row");
    row.appendChild(el("span", "card__source-num", toArabicDigits(index + 1)));
    const flag = sourceFlag(src.url);
    if (flag) row.appendChild(flag);

    if (src.url) {
      const link = el("a", null, sourceLabel(src));
      link.href = src.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      row.appendChild(link);
    } else {
      row.appendChild(el("span", "card__source-ref", sourceLabel(src)));
    }
    box.appendChild(row);
  });
  return box;
}

function renderAnswer(node, data) {
  node.textContent = "";
  const trail = crumbs(data);
  if (trail) node.appendChild(trail);

  const card = el("div", "card");

  const head = el("div", "card__head");
  if (data.service_name) head.appendChild(el("div", "card__service", data.service_name));
  if (data.authority)    head.appendChild(el("div", "card__authority", data.authority));
  if (data.confidence && CONF_LABEL[data.confidence]) {
    head.appendChild(el("span", "conf conf--" + data.confidence, CONF_LABEL[data.confidence]));
  }
  if (head.childNodes.length) card.appendChild(head);

  const body = renderMarkdown(data.answer);
  card.appendChild(body);
  node.appendChild(card);

  // الاستشهاد لا يظهر إلا بعد اكتمال النص، حتى لا يجاور إجابة نصف مكتوبة.
  const reveal = typeOut(body, () => {
    const sources = cardSources(data);
    if (sources) { card.appendChild(sources); scrollToEnd(); }
  });

  const skip = () => { reveal(); card.removeEventListener("click", skip); };
  card.addEventListener("click", skip);
}

function renderClarify(node, data) {
  node.textContent = "";
  node.appendChild(el("div", "msg__text", data.reply));

  const options = el("div", "options");
  (data.options || []).forEach((option) => {
    const btn = el("button", "option", option.label_ar);
    btn.type = "button";
    btn.addEventListener("click", () => {
      options.querySelectorAll("button").forEach((b) => (b.disabled = true));
      send(option.label_ar, option.service_id);
    });
    options.appendChild(btn);
  });
  node.appendChild(options);
}

function renderReply(node, data) {
  node.classList.remove("msg--refusal", "msg--error");

  if (data.format === "answer" || data.format === "card") {
    renderAnswer(node, data);
  } else if (data.format === "clarify") {
    clarifyCount += 1;
    renderClarify(node, data);
  } else {
    node.textContent = "";
    if (data.format === "refusal") node.classList.add("msg--refusal");
    node.appendChild(el("div", "msg__text", data.reply || data.text || ""));
    if (data.format === "prose") {
      const sources = cardSources(data);
      if (sources) node.appendChild(sources);
    }
  }

  if (typeof data.answer_time === "number") {
    node.appendChild(el("div", "msg__meta",
      `أُجيب خلال ${toArabicDigits(data.answer_time)} ثانية`));
  }

  // شريط السند يتبع آخر رد دائماً.
  if (data.format === "clarify") resetEvidence("بانتظار تحديد الخدمة");
  else renderEvidence(data);

  markCurrentService(data.service_name);
  scrollToEnd();
}

function renderError(node, message, serviceId) {
  node.textContent = "";
  node.classList.remove("msg--refusal");
  node.classList.add("msg--error");
  node.appendChild(el("div", "msg__text", message));

  const btn = el("button", "retry", "أعد المحاولة");
  btn.type = "button";
  btn.addEventListener("click", () => { node.remove(); deliver(serviceId); });
  node.appendChild(btn);
  scrollToEnd();
}

/* ------------------------------------------------------------ دورة الطلب */

async function deliver(serviceId) {
  if (busy) return;
  setBusy(true);
  const placeholder = showLoading();

  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        history: history,
        service_id: serviceId || null,
        clarify_count: clarifyCount,
      }),
    });

    let data = null;
    try { data = await response.json(); } catch (e) { data = null; }

    if (placeholder._stopThinking) placeholder._stopThinking();

    // 429 و500 يصلان نصاً عربياً من الخادم: يُعرض كما هو، لكن كفشل مع إعادة محاولة.
    if (!response.ok) {
      renderError(placeholder, (data && data.reply) || NETWORK_ERROR, serviceId);
      return;
    }

    renderReply(placeholder, data);
    history.push({
      role: "assistant",
      content: data.reply || data.text || data.service_name || "",
    });
  } catch (error) {
    if (placeholder._stopThinking) placeholder._stopThinking();
    renderError(placeholder, NETWORK_ERROR, serviceId);
  } finally {
    setBusy(false);
    inputEl.focus();
  }
}

function send(text, serviceId) {
  if (busy) return;

  if (!shellEl.classList.contains("is-chatting")) {
    shellEl.classList.add("is-chatting");
  }
  if (isMobile()) setMobilePanel(null);

  const userNode = bubble("user");
  userNode.appendChild(el("div", "msg__text", text));
  history.push({ role: "user", content: text });

  inputEl.value = "";
  deliver(serviceId);
}

formEl.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = inputEl.value.trim();
  if (text) send(text, null);
});

/* ---------------------------------------------------------------- الفهرس */

function arabicCount(n, forms) {
  if (n === 1) return forms.one;
  if (n === 2) return forms.two;
  return `${toArabicDigits(n)} ${n <= 10 ? forms.few : forms.many}`;
}
const SERVICE_WORDS   = { one: "خدمة واحدة", two: "خدمتان", few: "خدمات", many: "خدمة" };
const AUTHORITY_WORDS = { one: "جهة واحدة", two: "جهتان", few: "جهات", many: "جهة" };

// مرآة مُصغَّرة لطيّ الحروف في rag/arabic.py — لصندوق البحث وحده، لا لمسار
// الاسترجاع: نسخة في المتصفّح قد تنحرف وتُفسد الاسترجاع بصمت.
const AR_FOLD = { "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي" };

function normalizeQuery(text) {
  return (text || "")
    .normalize("NFKC")
    .replace(/[\u064B-\u0652\u0670\u0656-\u065F\u0640]/g, "")
    .replace(/[\u0660-\u0669]/g, (d) => String(d.charCodeAt(0) - 0x0660))
    .replace(/[أإآٱةى]/g, (c) => AR_FOLD[c])
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

const dirDataEl   = document.getElementById("directory-data");
const dirListEl   = document.getElementById("directory-list");
const dirSearchEl = document.getElementById("directory-search");
const dirEmptyEl  = document.getElementById("directory-empty");

let dirEntries = [];

function setExpanded(entry, expanded) {
  entry.card.classList.toggle("is-open", expanded);
  entry.head.setAttribute("aria-expanded", expanded ? "true" : "false");
}
const isExpanded = (entry) => entry.card.classList.contains("is-open");

function buildAuthority(authority, index) {
  const card = el("article", "auth");
  const panelId = `services-${authority.id}`;

  const head = el("button", "auth__head");
  head.type = "button";
  head.setAttribute("aria-expanded", "false");
  head.setAttribute("aria-controls", panelId);

  const bar = el("span", "auth__bar");
  bar.style.setProperty("--sector", SECTORS[index % SECTORS.length]);
  bar.setAttribute("aria-hidden", "true");

  const label = el("span", "auth__label");
  label.appendChild(el("span", "auth__name", authority.name_ar));
  label.appendChild(el("span", "auth__count",
    arabicCount(authority.services.length, SERVICE_WORDS)));
  const chev = icon(["m6 9 6 6 6-6"]);
  chev.setAttribute("class", "auth__chev");
  label.appendChild(chev);

  head.append(bar, label);

  const panel = el("div", "auth__panel");
  panel.id = panelId;
  const inner = el("div", "auth__inner");   // طبقة واحدة فقط: الشبكة تتحرّك 0fr→1fr

  const list = el("ul");
  const rows = authority.services.map((service) => {
    const node = el("li", "svc");
    node.appendChild(el("span", "svc__name", service.name_ar));
    const ask = el("button", "svc__ask", "اسأل");
    ask.type = "button";
    ask.title = `اسأل عن: ${service.name_ar}`;
    // نفس عقد الخيار المنقور: الخدمة اختيرت بالاسم، فيُمرَّر service_id.
    ask.addEventListener("click", () => send(service.name_ar, service.service_id));
    node.appendChild(ask);
    list.appendChild(node);
    return { service, node };
  });
  inner.appendChild(list);

  if (authority.portal_url) {
    const link = el("a", "auth__portal", "البوابة الرسمية للجهة");
    link.href = authority.portal_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    inner.appendChild(link);
  }
  panel.appendChild(inner);

  const entry = { authority, card, head, panel, rows };
  head.addEventListener("click", () => setExpanded(entry, !isExpanded(entry)));

  card.append(head, panel);
  return entry;
}

function markCurrentService(name) {
  if (!name) return;
  dirEntries.forEach((entry) => {
    let hit = false;
    entry.rows.forEach((row) => {
      const on = row.service.name_ar === name;
      row.node.classList.toggle("is-current", on);
      if (on) hit = true;
    });
    if (hit) setExpanded(entry, true);
  });
}

function applyFilter(raw) {
  const query = normalizeQuery(raw);
  let visible = 0;

  dirEntries.forEach((entry) => {
    // إصابة الجهة تعرض خدماتها كلها: من يبحث «شرطة عمان» يريد قائمتها كاملة.
    const authorityHit = !query || String(entry.authority.search || "").includes(query);
    let matches = 0;

    entry.rows.forEach((row) => {
      const hit = authorityHit || String(row.service.search || "").includes(query);
      row.node.hidden = !hit;
      if (hit) matches += 1;
    });

    const show = authorityHit || matches > 0;
    entry.card.hidden = !show;
    if (show) visible += 1;
    setExpanded(entry, Boolean(query) && show);
  });

  dirEmptyEl.hidden = visible > 0;
}

function initDirectory() {
  if (!dirDataEl || !dirListEl) return;

  let authorities = [];
  try { authorities = JSON.parse(dirDataEl.textContent) || []; } catch (e) { authorities = []; }
  if (!authorities.length) {
    dirEmptyEl.hidden = false;
    dirEmptyEl.textContent = "لم يتم تحميل الفهرس.";
    return;
  }

  dirEntries = authorities.map(buildAuthority);
  dirEntries.forEach((entry) => dirListEl.appendChild(entry.card));

  const serviceTotal = authorities.reduce((sum, a) => sum + a.services.length, 0);

  // أرقام الترحيب والشريحة المطويّة تأتي من الفهرس نفسه، لا من ثابت في القالب.
  const figAuth = document.getElementById("fig-auth");
  const figSvc  = document.getElementById("fig-svc");
  if (figAuth) figAuth.textContent = toArabicDigits(authorities.length);
  if (figSvc)  figSvc.textContent  = toArabicDigits(serviceTotal);
  if (foldIdxEl) foldIdxEl.textContent = toArabicDigits(serviceTotal);

  // ترتيب بديل: الأكثر خدمات.
  const sorted = dirEntries.slice().sort(
    (a, b) => b.authority.services.length - a.authority.services.length
  );
  document.querySelectorAll("[data-sort]").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll("[data-sort]").forEach((t) => {
        t.classList.toggle("is-active", t === tab);
        t.setAttribute("aria-selected", t === tab ? "true" : "false");
      });
      const order = tab.dataset.sort === "size" ? sorted : dirEntries;
      order.forEach((entry) => dirListEl.appendChild(entry.card));
    });
  });

  dirSearchEl.addEventListener("input",  () => applyFilter(dirSearchEl.value));
  dirSearchEl.addEventListener("search", () => applyFilter(dirSearchEl.value));
}

initDirectory();
resetEvidence();
inputEl.focus();
