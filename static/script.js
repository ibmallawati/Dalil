// Daleel frontend.
//
// The backend is stateless: the whole conversation is sent on every turn.
// Two extra fields travel with it —
//   service_id     the citizen tapped a clarifying option; that is their
//                  answer, so the backend goes straight to that service.
//   clarify_count  how many clarifying turns have already been spent.
//
// D4's "at most one clarifying turn" is enforced SERVER-side using that
// count. This file reports it honestly; it does not police it. A rule that
// lives only in browser state stops existing on refresh.

const shellEl = document.getElementById("shell");
const heroEl = document.getElementById("hero");
const messagesEl = document.getElementById("messages");
const threadEl = document.getElementById("thread");
const formEl = document.getElementById("composer");
const inputEl = document.getElementById("input");
const sendBtn = document.getElementById("send");
const servedByEl = document.getElementById("served-by");

const history = [];
let clarifyCount = 0;
let busy = false;
// The model at the head of the failover chain. The first answer defines it;
// anything different afterwards means we have fallen down the chain.
let primaryModel = null;

const FIELD_LABELS = {
  eligibility: "الفئة المستهدفة",
  required_documents: "المستندات المطلوبة",
  steps: "الخطوات",
  fees: "الرسوم",
  estimated_time: "المدة المتوقعة",
  where: "أين تتم المعاملة",
};
const ORDERED_FIELDS = [
  "eligibility", "required_documents", "steps",
  "fees", "estimated_time", "where",
];
const NOT_IN_SOURCE = "غير محدد في المصدر";
const CONF_LABEL = { high: "معلومات مؤكدة", partial: "معلومات جزئية" };
const NETWORK_ERROR = "تعذّر الاتصال بالخادم. تأكد من تشغيله ثم أعد المحاولة.";

const WELCOME =
  "مرحباً بك في دليل. اسألني عن إجراءات الخدمات الحكومية — " +
  "مثل تجديد جواز السفر أو البطاقة الشخصية أو السجل التجاري.";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
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

// --- loading state -------------------------------------------------------
// Three things move together, so they live in one place: the placeholder
// bubble, the composer lock, and aria-busy for screen readers. Answers take
// seconds, and a silent page during those seconds reads as broken.
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
  wrap.append(dots, el("span", "loading__label", "جارٍ البحث في المصادر الرسمية…"));
  node.appendChild(wrap);
  scrollToEnd();
  return node;
}

// Which model answered. Quiet, but present: mid-demo it says whether we are
// on the primary model or already down the failover chain.
function updateServedBy(model) {
  if (!model) return;
  if (!primaryModel) primaryModel = model;
  servedByEl.textContent = model;
  servedByEl.className =
    "badge " + (model === primaryModel ? "badge--primary" : "badge--fallback");
  servedByEl.title =
    model === primaryModel
      ? "النموذج الأساسي"
      : "نموذج احتياطي — تم التبديل تلقائياً";
}

function renderField(key, value) {
  const wrap = el("div", "field");
  wrap.appendChild(el("div", "field__label", FIELD_LABELS[key]));
  const body = el("div", "field__value");

  // An unsupported field is styled as a statement about the source rather
  // than as content. Dropping the row entirely would be worse: a checklist
  // with no «الرسوم» line reads as "this service is free".
  if (value === NOT_IN_SOURCE || value === null || value === undefined) {
    body.classList.add("field__value--missing");
    body.textContent = NOT_IN_SOURCE;
  } else if (Array.isArray(value)) {
    const list = el(key === "steps" ? "ol" : "ul");
    value.forEach((item) => list.appendChild(el("li", null, item)));
    body.appendChild(list);
  } else {
    body.textContent = value;
  }

  wrap.appendChild(body);
  return wrap;
}

// A citizen must be able to tell an official ministry page from a blog at a
// glance. The corpus now accepts arbitrary URLs via the scraper, so the two
// must not render identically.
//
// Derived from source_url here in the browser rather than from a backend
// field, deliberately: it needs no change to the request path, and the URL
// is already on every card.
function sourceBadge(url) {
  let host = "";
  try {
    host = new URL(url).hostname.toLowerCase();
  } catch (error) {
    return null;
  }
  const official = host === "gov.om" || host.endsWith(".gov.om");
  const badge = el(
    "span",
    "srcflag " + (official ? "srcflag--gov" : "srcflag--other"),
    official ? "مصدر حكومي رسمي" : "مصدر غير حكومي"
  );
  badge.title = host;
  return badge;
}

function sourceLine(data) {
  const src = el("div", "card__source");
  const flag = sourceBadge(data.source_url);
  if (flag) src.appendChild(flag);
  src.appendChild(el("span", null, "المصدر: "));
  const link = el("a", null, data.source_title || data.source_url);
  link.href = data.source_url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  src.appendChild(link);
  return src;
}

function renderCard(node, data) {
  node.innerHTML = "";
  const card = el("div", "card");

  const head = el("div", "card__head");
  if (data.service_name) head.appendChild(el("div", "card__service", data.service_name));
  if (data.authority) head.appendChild(el("div", "card__authority", data.authority));
  if (data.confidence && CONF_LABEL[data.confidence]) {
    head.appendChild(el("span", "conf conf--" + data.confidence, CONF_LABEL[data.confidence]));
  }
  card.appendChild(head);

  ORDERED_FIELDS.forEach((key) => card.appendChild(renderField(key, data[key])));

  // Every answer carries its source. This is the visible half of "how do
  // you know it isn't hallucinating" — and the URL comes from the corpus,
  // never from the model.
  if (data.source_url) card.appendChild(sourceLine(data));

  node.appendChild(card);
}

function renderClarify(node, data) {
  node.innerHTML = "";
  node.appendChild(el("div", "msg__text", data.reply));

  const options = el("div", "options");
  (data.options || []).forEach((option) => {
    const btn = el("button", "option", option.label_ar);
    btn.type = "button";
    btn.addEventListener("click", () => {
      // Disable the whole group: the question has been answered, and a
      // second tap would ask the same thing again.
      options.querySelectorAll("button").forEach((b) => (b.disabled = true));
      send(option.label_ar, option.service_id);
    });
    options.appendChild(btn);
  });
  node.appendChild(options);
}

function renderReply(node, data) {
  node.classList.remove("msg--refusal");

  if (data.format === "card") {
    renderCard(node, data);
  } else if (data.format === "clarify") {
    clarifyCount += 1;
    renderClarify(node, data);
  } else {
    // prose and refusal
    node.innerHTML = "";
    if (data.format === "refusal") node.classList.add("msg--refusal");
    node.appendChild(el("div", "msg__text", data.reply || data.text || ""));
    if (data.format === "prose" && data.source_url) node.appendChild(sourceLine(data));
  }

  if (typeof data.answer_time === "number") {
    node.appendChild(el("div", "msg__meta", `أُجيب خلال ${data.answer_time} ثانية`));
  }
  scrollToEnd();
}

// --- error state ---------------------------------------------------------
// Distinct from a refusal: a refusal is an answer ("I have no grounded
// source for that"), a failure is the request not completing. Retry replays
// the same history, so nothing is retyped and no turn is duplicated.
function renderError(node, message, serviceId) {
  node.innerHTML = "";
  node.classList.remove("msg--refusal");
  node.classList.add("msg--error");
  node.appendChild(el("div", "msg__text", message));

  const btn = el("button", "retry", "إعادة المحاولة");
  btn.type = "button";
  btn.addEventListener("click", () => {
    node.remove();
    deliver(serviceId);
  });
  node.appendChild(btn);
  scrollToEnd();
}

// One turn against the backend, using whatever is already in `history`.
// Kept separate from send() so retry does not re-append the user's message.
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
    try {
      data = await response.json();
    } catch (parseError) {
      data = null;
    }

    // Rate limits (429) and stage failures (500) still arrive as Arabic
    // JSON, so their text is shown as-is — but as a failure with a retry,
    // not as an answer.
    if (!response.ok) {
      renderError(placeholder, (data && data.reply) || NETWORK_ERROR, serviceId);
      return;
    }

    updateServedBy(data.served_by);
    renderReply(placeholder, data);

    // Only the visible reply goes back into the history. Card fields do
    // not: re-feeding a rendered checklist as conversational text invites
    // the model to treat its own earlier output as grounding.
    history.push({
      role: "assistant",
      content: data.reply || data.text || data.service_name || "",
    });
  } catch (error) {
    renderError(placeholder, NETWORK_ERROR, serviceId);
  } finally {
    setBusy(false);
    inputEl.focus();
  }
}

function send(text, serviceId) {
  if (busy) return;

  // The hero has done its job the moment a conversation starts.
  if (!shellEl.classList.contains("is-chatting")) {
    shellEl.classList.add("is-chatting");
    const greeting = bubble("bot");
    greeting.appendChild(el("div", "msg__text", WELCOME));
  }

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

heroEl.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => send(chip.dataset.q, null));
});

// --- browse: authorities and their services ------------------------------
// The coverage map, rendered. Every name here came from
// data/registry/authorities.json via registry.browse_directory() — nothing
// in this file knows the name of a single ministry.
//
// Tapping a service enters the SAME chat flow as typing, with its
// service_id attached: the citizen picked that service by name, so the
// backend loads the whole service rather than guessing by similarity.
// That is the existing tapped-option contract, reused as-is.

const directoryEl = document.getElementById("directory");
const dirDataEl = document.getElementById("directory-data");
const dirListEl = document.getElementById("directory-list");
const dirSearchEl = document.getElementById("directory-search");
const dirEmptyEl = document.getElementById("directory-empty");
const dirMetaEl = document.getElementById("directory-meta");

// Arabic counted nouns: 1 singular, 2 dual, 3-10 plural, 11+ singular
// again. «8 خدمة» is wrong in a way every Arabic reader notices.
function arabicCount(n, forms) {
  if (n === 1) return forms.one;
  if (n === 2) return forms.two;
  return `${n} ${n <= 10 ? forms.few : forms.many}`;
}
const SERVICE_WORDS = { one: "خدمة واحدة", two: "خدمتان", few: "خدمات", many: "خدمة" };
const AUTHORITY_WORDS = { one: "جهة واحدة", two: "جهتان", few: "جهات", many: "جهة" };

// A REDUCED mirror of the folding in rag/arabic.py, applied to the search
// box only. The registry side of every comparison was normalized in Python
// by normalize_arabic() itself, so this only has to fold what a citizen may
// type: diacritics, tatweel, the alef family, ة/ى, and Arabic-Indic digits.
//
// It is deliberately NOT on the retrieval path. normalize_arabic() is the
// one function both sides of embedding call; a browser copy of it could
// drift and silently degrade retrieval, so this one filters a visible list
// and nothing else.
const AR_FOLD = { "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي" };

function normalizeQuery(text) {
  return (text || "")
    .normalize("NFKC")
    .replace(/[ً-ْٰۖ-ۭـ]/g, "")
    .replace(/[٠-٩]/g, (d) => String(d.charCodeAt(0) - 0x0660))
    .replace(/[أإآٱةى]/g, (c) => AR_FOLD[c])
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

function setExpanded(entry, expanded) {
  entry.panel.hidden = !expanded;
  entry.head.setAttribute("aria-expanded", expanded ? "true" : "false");
}

function buildAuthority(authority) {
  const card = el("article", "auth");
  const panelId = `services-${authority.id}`;

  const head = el("button", "auth__head");
  head.type = "button";
  head.setAttribute("aria-expanded", "false");
  head.setAttribute("aria-controls", panelId);
  head.appendChild(el("span", "auth__name", authority.name_ar));
  head.appendChild(
    el("span", "auth__count", arabicCount(authority.services.length, SERVICE_WORDS))
  );
  const chev = el("span", "auth__chev", "▾");
  chev.setAttribute("aria-hidden", "true");
  head.appendChild(chev);

  const panel = el("div", "auth__panel");
  panel.id = panelId;
  panel.hidden = true;

  const list = el("ul", "svclist");
  const rows = authority.services.map((service) => {
    const node = el("li", "svc");
    node.appendChild(el("span", "svc__name", service.name_ar));

    const ask = el("button", "svc__ask", "اسأل عن هذه الخدمة");
    ask.type = "button";
    ask.title = `اسأل عن: ${service.name_ar}`;
    ask.addEventListener("click", () => send(service.name_ar, service.service_id));
    node.appendChild(ask);

    list.appendChild(node);
    return { service, node };
  });
  panel.appendChild(list);

  // The official portal, for the services this authority has that Daleel
  // does not document yet.
  if (authority.portal_url) {
    const link = el("a", "auth__portal", "البوابة الرسمية للجهة");
    link.href = authority.portal_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    panel.appendChild(link);
  }

  const entry = { authority, card, head, panel, rows };
  head.addEventListener("click", () => setExpanded(entry, panel.hidden));

  card.append(head, panel);
  return entry;
}

function initDirectory() {
  if (!directoryEl || !dirDataEl) return;

  let authorities = [];
  try {
    authorities = JSON.parse(dirDataEl.textContent) || [];
  } catch (error) {
    // A broken directory must not take the chat down with it: the composer
    // above is the primary way in, and it does not depend on this list.
    directoryEl.hidden = true;
    return;
  }
  if (!authorities.length) {
    directoryEl.hidden = true;
    return;
  }

  const entries = authorities.map(buildAuthority);
  entries.forEach((entry) => dirListEl.appendChild(entry.card));

  const serviceTotal = authorities.reduce((sum, a) => sum + a.services.length, 0);
  dirMetaEl.textContent =
    `${arabicCount(authorities.length, AUTHORITY_WORDS)} · ` +
    `${arabicCount(serviceTotal, SERVICE_WORDS)}`;

  function applyFilter(raw) {
    const query = normalizeQuery(raw);
    let visible = 0;

    entries.forEach((entry) => {
      // An authority hit shows all of its services: someone searching
      // «شرطة عمان» wants the ministry's whole list, not a filtered one.
      const authorityHit = !query || entry.authority.search.includes(query);
      let matches = 0;

      entry.rows.forEach((row) => {
        const hit = authorityHit || row.service.search.includes(query);
        row.node.hidden = !hit;
        if (hit) matches += 1;
      });

      const show = authorityHit || matches > 0;
      entry.card.hidden = !show;
      if (show) visible += 1;

      // While searching, open every surviving card so the match is on
      // screen without a second tap. Clearing the box restores the
      // collapsed default rather than leaving ten cards open.
      setExpanded(entry, Boolean(query) && show);
    });

    dirEmptyEl.hidden = visible > 0;
  }

  dirSearchEl.addEventListener("input", () => applyFilter(dirSearchEl.value));
  // type="search" clears via Escape and the native ✕ without firing input
  // in every browser.
  dirSearchEl.addEventListener("search", () => applyFilter(dirSearchEl.value));
}

initDirectory();

inputEl.focus();
