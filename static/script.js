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

const newChatEl = document.getElementById("new-chat");
const themeToggleEl = document.getElementById("theme-toggle");
const themeColorMetaEl = document.getElementById("theme-color-meta");
const menuToggleEl = document.getElementById("menu-toggle");
const drawerCloseEl = document.getElementById("drawer-close");
const drawerBackdropEl = document.getElementById("drawer-backdrop");
const openDirectoryEl = document.getElementById("open-directory");

const history = [] ;
let clarifyCount = 0;
let busy = false;
// Set by initDirectory() once the coverage payload is parsed. renderAnswer()
// reads it to offer follow-up services from the SAME authority — no second
// request, no backend change, just the payload the browse grid already has.
let directoryAuthorities = [];

// The answer is free-form now: no fixed field list, no «غير محدد» padding.
// The model returns Arabic text in a narrow markdown subset and the shape
// follows the question — a fee question gets a fee, not six sections.
const CONF_LABEL = { high: "معلومات مؤكدة", partial: "معلومات جزئية" };
const NETWORK_ERROR = "تعذّر الاتصال بالخادم. تأكد من تشغيله ثم أعد المحاولة.";

const WELCOME =
  "مرحباً بك في دليل. اسألني عن إجراءات الخدمات الحكومية — " +
  "مثل تجديد جواز السفر أو البطاقة الشخصية أو السجل التجاري.";

// --- theme (system / light / dark, persisted) -----------------------------
const THEME_KEY = "daleel:theme";
const THEME_TITLE = { system: "المظهر: تلقائي", light: "المظهر: فاتح", dark: "المظهر: داكن" };
const THEME_COLOR = { system: "#f5f8fd", light: "#f5f8fd", dark: "#070c16" };
const darkMediaQuery = window.matchMedia("(prefers-color-scheme: dark)");

function currentTheme() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch (error) {}
  return "system";
}

function applyTheme(theme) {
  if (theme === "light" || theme === "dark") {
    document.documentElement.setAttribute("data-theme", theme);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
  themeToggleEl.dataset.theme = theme;
  themeToggleEl.title = THEME_TITLE[theme];
  const resolved = theme === "system" ? (darkMediaQuery.matches ? "dark" : "light") : theme;
  themeColorMetaEl.setAttribute("content", THEME_COLOR[resolved]);
}

applyTheme(currentTheme());
darkMediaQuery.addEventListener("change", () => {
  if (currentTheme() === "system") applyTheme("system");
});

themeToggleEl.addEventListener("click", () => {
  const order = ["system", "light", "dark"];
  const next = order[(order.indexOf(currentTheme()) + 1) % order.length];
  try { localStorage.setItem(THEME_KEY, next); } catch (error) {}
  applyTheme(next);
});

// --- new chat --------------------------------------------------------------
// The backend is stateless per-request; a fresh conversation is just a
// reload, which resets both this file's in-memory state and the rendered
// thread in one step.
newChatEl.addEventListener("click", () => {
  if (busy) return;
  location.reload();
});

// --- directory drawer -------------------------------------------------------
// The index is opened on demand rather than living inline on the homepage.
function setDrawer(open) {
  shellEl.classList.toggle("drawer-open", open);
  menuToggleEl.setAttribute("aria-expanded", open ? "true" : "false");
}
menuToggleEl.addEventListener("click", () => setDrawer(!shellEl.classList.contains("drawer-open")));
drawerCloseEl.addEventListener("click", () => setDrawer(false));
drawerBackdropEl.addEventListener("click", () => setDrawer(false));
if (openDirectoryEl) openDirectoryEl.addEventListener("click", () => setDrawer(true));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && shellEl.classList.contains("drawer-open")) setDrawer(false);
});

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function scrollToEnd() {
  threadEl.scrollTop = threadEl.scrollHeight;
}

// --- avatars ---------------------------------------------------------------
// A small, fixed mark per role — the sparkle for دليل, a plain person glyph
// for the citizen — so who is speaking is legible without reading the text.
const BOT_AVATAR_SVG =
  '<svg viewBox="0 0 24 24" fill="currentColor"><polygon points="12,3 14,10 21,12 14,14 12,21 10,14 3,12 10,10"/></svg>';
const USER_AVATAR_SVG =
  '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="8.5" r="4"/>' +
  '<path d="M4 20c0-4.4 3.6-8 8-8s8 3.6 8 8Z"/></svg>';

function bubble(role) {
  const isUser = role === "user";
  const row = el("div", "msg " + (isUser ? "msg--user" : "msg--bot"));

  const avatar = el("div", "msg__avatar " + (isUser ? "msg__avatar--user" : "msg__avatar--bot"));
  avatar.setAttribute("aria-hidden", "true");
  avatar.innerHTML = isUser ? USER_AVATAR_SVG : BOT_AVATAR_SVG;

  const content = el("div", "msg__content");

  row.append(avatar, content);
  messagesEl.appendChild(row);
  scrollToEnd();
  return content;
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
        // Numbered steps get the circled-counter treatment (see CSS
        // .answer__list.is-ordered) — the one place an answer's structure is
        // worth showing, since «الخطوات» is read in order.
        list = el(wantTag, "answer__list" + (numbered ? " is-ordered" : ""));
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
  // more than one citation. Rendered as a numbered list — a research-answer
  // convention that also makes several sources easy to tell apart.
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

  list.forEach((src, index) => {
    const row = el("div", "card__source-row");
    row.appendChild(el("span", "srcnum", String(index + 1)));

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

// Perplexity-style follow-ups: once an answer names its authority, its
// siblings in the SAME authority (from the directory payload already parsed
// by initDirectory()) become one-tap next questions. Purely a client-side
// lookup by name — the backend response has no service_id, only the display
// name and authority text, which is what the directory rows carry too.
function relatedChips(data) {
  if (!data.authority || !directoryAuthorities.length) return null;
  const authority = directoryAuthorities.find((a) => a.name_ar === data.authority);
  if (!authority) return null;

  // Exact equality is not enough: the model paraphrases card titles (e.g.
  // "تجديد جواز السفر العماني" for the directory's "تجديد جواز السفر"), so
  // the current service must be excluded by substring match in EITHER
  // direction, not by exact string equality.
  const current = data.service_name || "";
  const siblings = authority.services
    .filter((s) => !current.includes(s.name_ar) && !s.name_ar.includes(current))
    .slice(0, 3);
  if (!siblings.length) return null;

  const wrap = el("div", "related");
  wrap.appendChild(el("div", "related__label", "قد يهمك أيضاً"));
  const list = el("div", "related__list");
  siblings.forEach((service) => {
    const btn = el("button", "related__chip", service.name_ar);
    btn.type = "button";
    btn.addEventListener("click", () => send(service.name_ar, service.service_id));
    list.appendChild(btn);
  });
  wrap.appendChild(list);
  return wrap;
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

  const related = relatedChips(data);
  if (related) card.appendChild(related);

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

  // Picking a service from the drawer should return the citizen to the
  // conversation, not leave the drawer covering the answer that arrives.
  setDrawer(false);

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
const heroStatsEl = document.getElementById("hero-stats");

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

// --- category icons -------------------------------------------------------
// A small hand-drawn line-icon set (no icon font, no image request), matched
// to an authority by its stable registry id. Several ids share a theme
// (finance/economy, education/culture) rather than each getting a bespoke
// glyph — the point is visual variety across the grid, not a 1:1 taxonomy.
const ICONS = {
  shield: '<path d="M12 3 19 6.2V11c0 5-3.2 8.6-7 10-3.8-1.4-7-5-7-10V6.2Z"/>',
  briefcase: '<rect x="3" y="7.5" width="18" height="11.5" rx="2"/><path d="M8.5 7.5V6a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2v1.5"/><path d="M3 13h18"/>',
  health: '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
  home: '<path d="M4 11 12 4l8 7"/><path d="M6 10v10h12V10"/><path d="M10 20v-6h4v6"/>',
  coin: '<circle cx="9" cy="9" r="6"/><circle cx="15" cy="15" r="6" opacity=".55"/>',
  book: '<path d="M4 5.5C6.5 3.8 9.5 3.8 12 5.5 14.5 3.8 17.5 3.8 20 5.5V18.5C17.5 16.8 14.5 16.8 12 18.5 9.5 16.8 6.5 16.8 4 18.5Z"/><path d="M12 5.5V18.5"/>',
  leaf: '<path d="M6 20C6 10 14 4 20 4 20 12 14 20 6 20Z"/><path d="M6.5 19.5C10 14 14 10 19.5 4.5"/>',
  globe: '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
};
const AUTHORITY_ICON = {
  rop: "shield", moi: "shield", moj: "shield",
  mociip: "briefcase", mol: "briefcase", moec: "briefcase",
  moh: "health",
  mohup: "home",
  mof: "coin",
  moe: "book", mcsy: "book",
  mafwr: "leaf", mem: "leaf",
};

function iconFor(authority) {
  return ICONS[AUTHORITY_ICON[authority.id] || "globe"] || ICONS.globe;
}

// Open/closed state lives in a class rather than in the `hidden` attribute,
// because `hidden` is display:none and display cannot be transitioned. The
// CSS animates the panel with grid-template-rows 0fr -> 1fr (so it grows to
// exactly its own content height, even after the filter changes how many
// rows are in it) and uses `visibility` to keep a closed panel out of the
// tab order and the accessibility tree.
function setExpanded(entry, expanded) {
  entry.card.classList.toggle("is-open", expanded);
  entry.head.setAttribute("aria-expanded", expanded ? "true" : "false");
}
function isExpanded(entry) {
  return entry.card.classList.contains("is-open");
}

function buildAuthority(authority, index) {
  const card = el("article", "auth");
  card.style.setProperty("--i", String(Math.min(index, 14)));
  const panelId = `services-${authority.id}`;

  const head = el("button", "auth__head");
  head.type = "button";
  head.setAttribute("aria-expanded", "false");
  head.setAttribute("aria-controls", panelId);

  const icon = el("span", "auth__icon");
  icon.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${iconFor(authority)}</svg>`;
  icon.setAttribute("aria-hidden", "true");
  head.appendChild(icon);

  const meta = el("div", "auth__meta");
  meta.appendChild(el("h3", "auth__name", authority.name_ar));
  meta.appendChild(el("span", "auth__count", arabicCount(authority.services.length, SERVICE_WORDS)));
  head.appendChild(meta);

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
  const rows = authority.services.map((service) => {
    const row = el("li", "svc");
    row.appendChild(el("span", "svc__name", service.name_ar));

    const ask = el("button", "svc__ask", "اسأل");
    ask.type = "button";
    ask.title = `اسأل عن: ${service.name_ar}`;
    ask.addEventListener("click", () => send(service.name_ar, service.service_id));
    row.appendChild(ask);

    list.appendChild(row);
    return { service, node: row };
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
    authorities = [];
  }
  if (!authorities.length) {
    // A broken/empty directory must not take the chat down with it: the
    // composer is the primary way in and does not depend on this list. Hide
    // the drawer and every button that would otherwise open an empty one.
    directoryEl.hidden = true;
    menuToggleEl.hidden = true;
    if (openDirectoryEl) openDirectoryEl.hidden = true;
    return;
  }
  directoryAuthorities = authorities;

  const entries = authorities.map(buildAuthority);
  entries.forEach((entry) => dirListEl.appendChild(entry.card));

  const serviceTotal = authorities.reduce((sum, a) => sum + a.services.length, 0);
  heroStatsEl.replaceChildren(
    (() => {
      const s = el("div", "stat");
      s.append(el("span", "stat__value", String(authorities.length)),
               el("span", "stat__label", "جهة حكومية"));
      return s;
    })(),
    (() => {
      const s = el("div", "stat");
      s.append(el("span", "stat__value", String(serviceTotal)),
               el("span", "stat__label", "خدمة موثّقة"));
      return s;
    })()
  );

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
      // collapsed default rather than leaving every card open.
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
