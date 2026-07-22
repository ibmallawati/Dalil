// Grab the page elements we need.
const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("composer");
const inputEl = document.getElementById("input");
const sendBtn = document.getElementById("send");

// The full conversation. We send this to the backend on every turn,
// because the model has no memory of its own.
const history = [];

// Add a message bubble to the screen.
// `meta` is optional — when present (e.g. "answered in 2.31s"), it's shown
// as a small muted line under the message text. Only bot bubbles use this.
function addMessage(role, text, meta) {
  const bubble = document.createElement("div");
  bubble.className = "msg " + (role === "user" ? "msg--user" : "msg--bot");

  const textEl = document.createElement("div");
  textEl.className = "msg__text";
  textEl.textContent = text;
  bubble.appendChild(textEl);

  if (meta) {
    const metaEl = document.createElement("div");
    metaEl.className = "msg__meta";
    metaEl.textContent = meta;
    bubble.appendChild(metaEl);
  }

  messagesEl.appendChild(bubble);
  messagesEl.scrollTop = messagesEl.scrollHeight; // scroll to newest
  return bubble;
}

// Show an animated "typing..." bubble while we wait for the model.
function showTyping() {
  const bubble = document.createElement("div");
  bubble.className = "msg msg--bot";
  bubble.innerHTML =
    '<span class="typing"><span></span><span></span><span></span></span>';
  messagesEl.appendChild(bubble);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return bubble;
}

// Replace a typing bubble's contents with the real reply + optional timing.
function fillBubble(bubble, text, meta) {
  bubble.innerHTML = ""; // clear the typing dots

  const textEl = document.createElement("div");
  textEl.className = "msg__text";
  textEl.textContent = text;
  bubble.appendChild(textEl);

  if (meta) {
    const metaEl = document.createElement("div");
    metaEl.className = "msg__meta";
    metaEl.textContent = meta;
    bubble.appendChild(metaEl);
  }
}

// Runs when the user sends a message.
async function handleSend(event) {
  event.preventDefault();
  const text = inputEl.value.trim();
  if (!text) return;

  // Show the user's message and remember it.
  addMessage("user", text);
  history.push({ role: "user", content: text });
  inputEl.value = "";
  sendBtn.disabled = true;

  const typingBubble = showTyping();

  try {
    // Send the whole conversation to our backend.
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ history: history }),
    });
    const data = await response.json();

    // Build the timing line, if the backend sent one.
    // (The error path in app.py doesn't include answer_time, so this
    // stays blank rather than printing "undefined".)
    const meta =
      typeof data.answer_time === "number"
        ? `⏱ answered in ${data.answer_time}s`
        : null;

    fillBubble(typingBubble, data.reply, meta);
    history.push({ role: "assistant", content: data.reply });
  } catch (error) {
    fillBubble(typingBubble, "Network error. Is the server running?", null);
  } finally {
    sendBtn.disabled = false;
    inputEl.focus();
  }
}

formEl.addEventListener("submit", handleSend);