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

// The answer is free-form now: no fixed field list, no «غير محدد» padding.
// The model returns Arabic text in a narrow markdown subset and the shape
// follows the question — a fee question gets a fee, not six sections.
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

// --- markdown subset -----------------------------------------------------
// The model may emit "- item", "1. step", and **bold**. Everything is built
// as real DOM nodes, never innerHTML: the text is model output and must not
// be able to inject markup. A full markdown library would be a dependency
// for three constructs, and this project is kept deliberately small.

function appendInline(parent, text) {
  // Split on **bold** and emit alternating text / <strong> nodes.
  const parts = String(text).split(/\*\*(.+?)\*\*/g);
  parts.forEach((part, i) => {
    if (!part) return;
    if (i % 2 === 1) parent.appendChild(el("strong", null, part));
    else parent.appendChild(document.createTextNode(part));
  });
}

function renderMarkdown(text) {
  const wrap = el("div", "answer__body");
  const lines = String(text || "").split("\n");
  let list = null;

  const closeList = () => { list = null; };

  lines.forEach((raw) => {
    const line = raw.trim();
    if (!line) { closeList(); return; }

    const bullet = line.match(/^[-•*]\s+(.*)$/);
    const numbered = line.match(/^(\d+)[.)]\s+(.*)$/);

    if (bullet || numbered) {
      const wantTag = numbered ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== wantTag) {
        list = el(wantTag, "answer__list");
        wrap.appendChild(list);
      }
      const li = el("li");
      appendInline(li, (bullet ? bullet[1] : numbered[2]));
      list.appendChild(li);
      return;
    }

    closeList();
    const p = el("p", "answer__p");
    appendInline(p, line);
    wrap.appendChild(p);
  });

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

function sourcesBlock(data) {
  // Retrieval can span several services, so an answer can legitimately have
  // more than one citation. Rendered as a list rather than a single line.
  const list = (data.sources && data.sources.length)
    ? data.sources
    : (data.source_url || data.source_title
        ? [{ url: data.source_url, title: data.source_title,
             ref: data.source_title, service: data.service_name,
             authority: data.authority }]
        : []);
  if (!list.length) return null;

  const box = el("div", "card__source");
  box.appendChild(el("div", "card__source-label",
    list.length > 1 ? "المصادر:" : "المصدر:"));

  list.forEach((src) => {
    const row = el("div", "card__source-row");
    const flag = sourceBadge(src.url);
    if (flag) row.appendChild(flag);

    const label = [src.authority, src.service].filter(Boolean).join(" — ")
      || src.title || src.ref || src.url;

    if (src.url) {
      const link = el("a", null, label);
      link.href = src.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      row.appendChild(link);
    } else {
      // Textual attribution: a printed guide or a PDF with no live page.
      // Not clickable, still honest about where the answer came from.
      row.appendChild(el("span", "card__source-ref", label));
    }
    box.appendChild(row);
  });
  return box;
}

function renderAnswer(node, data) {
  node.innerHTML = "";
  const card = el("div", "card");

  const head = el("div", "card__head");
  if (data.service_name) head.appendChild(el("div", "card__service", data.service_name));
  if (data.authority) head.appendChild(el("div", "card__authority", data.authority));
  if (data.confidence && CONF_LABEL[data.confidence]) {
    head.appendChild(el("span", "conf conf--" + data.confidence, CONF_LABEL[data.confidence]));
  }
  if (head.childNodes.length) card.appendChild(head);

  card.appendChild(renderMarkdown(data.answer));

  // Every answer carries its source. This is the visible half of "how do
  // you know it isn't hallucinating" — and it comes from the corpus, never
  // from the model.
  const sources = sourcesBlock(data);
  if (sources) card.appendChild(sources);

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

  if (data.format === "answer" || data.format === "card") {
    renderAnswer(node, data);
  } else if (data.format === "clarify") {
    clarifyCount += 1;
    renderClarify(node, data);
  } else {
    // prose and refusal
    node.innerHTML = "";
    if (data.format === "refusal") node.classList.add("msg--refusal");
    node.appendChild(el("div", "msg__text", data.reply || data.text || ""));
    if (data.format === "prose") {
      const sources = sourcesBlock(data);
      if (sources) node.appendChild(sources);
    }
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

// Open/closed state lives in a class rather than in the `hidden` attribute,
// because `hidden` is display:none and display cannot be transitioned. The
// CSS animates the panel with grid-template-rows 0fr -> 1fr (so it grows to
// exactly its own content height, even after the filter changes how many
// rows are in it) and uses `visibility` to keep a closed panel out of the
// tab order and the accessibility tree — the job `hidden` was doing.
//
// aria-expanded still carries the state for assistive tech, unchanged.
function setExpanded(entry, expanded) {
  entry.card.classList.toggle("is-open", expanded);
  entry.head.setAttribute("aria-expanded", expanded ? "true" : "false");
}

function isExpanded(entry) {
  return entry.card.classList.contains("is-open");
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

  // Single wrapper inside the panel, and it has to stay single: the panel is
  // the animating grid, and a second child would land in an implicit row
  // that the 0fr collapse does not cover — the list would clip while the
  // portal link stayed visible.
  const inner = el("div", "auth__inner");

  const list = el("ul", "svclist");
  const rows = authority.services.map((service, index) => {
    const node = el("li", "svc");
    // Staggers this row's entrance as the panel unfolds.
    node.style.setProperty("--j", String(index));
    node.appendChild(el("span", "svc__name", service.name_ar));

    const ask = el("button", "svc__ask", "اسأل عن هذه الخدمة");
    ask.type = "button";
    ask.title = `اسأل عن: ${service.name_ar}`;
    ask.addEventListener("click", () => send(service.name_ar, service.service_id));
    node.appendChild(ask);

    list.appendChild(node);
    return { service, node };
  });
  inner.appendChild(list);

  // The official portal, for the services this authority has that Daleel
  // does not document yet.
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
  entries.forEach((entry, index) => {
    // --i drives the staggered entrance in CSS, so the grid assembles in
    // sequence instead of ten cards landing on the same frame. Capped, or a
    // future eleventh authority would wait most of a second to appear.
    entry.card.style.setProperty("--i", String(Math.min(index, 11)));
    dirListEl.appendChild(entry.card);
  });

  const serviceTotal = authorities.reduce((sum, a) => sum + a.services.length, 0);
  dirMetaEl.textContent =
    `${arabicCount(authorities.length, AUTHORITY_WORDS)} · ` +
    `${arabicCount(serviceTotal, SERVICE_WORDS)}`;

  // Replaying the card entrance on every keystroke would flicker — «جواز» is
  // four of them. So the animation is tied to the RESULT SET, not to typing:
  // it replays only when which cards are visible actually changes.
  let lastSignature = null;

  function animateResultChange(signature) {
    if (signature === lastSignature) return;
    lastSignature = signature;
    dirListEl.classList.remove("is-refiltered");
    // One forced reflow. Without it the browser coalesces the remove+add
    // into no change at all and the animation never restarts.
    void dirListEl.offsetWidth;
    dirListEl.classList.add("is-refiltered");
  }

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
    animateResultChange(entries.map((e) => (e.card.hidden ? "0" : "1")).join(""));
  }

  dirSearchEl.addEventListener("input", () => applyFilter(dirSearchEl.value));
  // type="search" clears via Escape and the native ✕ without firing input
  // in every browser.
  dirSearchEl.addEventListener("search", () => applyFilter(dirSearchEl.value));
}

initDirectory();

inputEl.focus();
