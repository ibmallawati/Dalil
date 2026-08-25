# Prompt for Claude Code

Paste this into Claude Code from the repo root, on branch `First-trial`.

---

You are working in `ibmallawati/eai`, a Flask app (Arabic RTL, Omani government
service assistant called دليل). Work **only on the branch `First-trial`** — never
check out, commit to, or modify `main` or any other branch.

A redesigned front end is provided in `design_handoff_daleel_ui/`. It is written
for this exact stack (Jinja template + one plain CSS file + one vanilla JS file,
no framework, no build step — the app runs on a 512MB Render instance).

## Task

1. Read `design_handoff_daleel_ui/README.md` first — it documents the structure,
   the design tokens, and the one optional backend field.
2. Replace the three front-end files with the versions in the bundle:
   - `design_handoff_daleel_ui/templates/index.html` → `templates/index.html`
   - `design_handoff_daleel_ui/static/style.css` → `static/style.css`
   - `design_handoff_daleel_ui/static/script.js` → `static/script.js`
   Keep the old files as `*.bak` (or rely on git) so the previous UI is
   recoverable.
3. Do **not** change the API contract. The new front end calls exactly:
   - `POST /chat` with `{ history, service_id, clarify_count }`
   - and reads `{{ directory | tojson }}` from the template context, in the same
     shape `registry.browse_directory()` already returns
     (`id`, `name_ar`, `portal_url`, `search`, `services[].service_id`,
     `services[].name_ar`, `services[].search`).
   If `app.py` passes the directory under a different variable name, adapt the
   template's `{{ directory | tojson }}` line — not the JS.
4. `GET /usage` is no longer called from the browser: the on-screen quota meter
   was removed by the owner's request. Leave the route and the server-side rate
   limiter untouched. A 429 must still surface its Arabic `reply` text as an
   error message with a retry button (the JS already does this).
5. Run the app and verify, in Arabic RTL:
   - home state (index rail populated, welcome copy, no answer yet)
   - ask a question → answer card, breadcrumb, numbered sources in the left rail
   - tap a service in the index rail → same flow with `service_id` attached
   - a clarify reply renders tappable options and does not fabricate an answer
   - a refusal renders as the clay panel, not as an answer
   - stop the backend → error panel + «أعد المحاولة» replays the same history
   - fold/unfold each rail with the ☰ button; state survives a reload
   - narrow the window under 960px → rails become sliding panels, bottom bar appears
   - toggle dark mode; reload and confirm it persists
6. Report anything in the design that the current backend cannot support, and do
   not invent data to fill it.

## Hard constraints

- **No new dependencies.** No React, no Tailwind, no bundler, no CSS
  preprocessor, no icon package. Icons are inline SVG; the only external asset
  is the IBM Plex Sans Arabic webfont already linked in the template.
- **No English in the UI.** Every visible string and every digit must be Arabic
  (Arabic-Indic numerals — use the `toArabicDigits` helper in `script.js`).
  Comments in the source may be Arabic or English.
- **Never render model output with `innerHTML`.** The existing markdown-subset
  renderer builds real DOM nodes on purpose; keep it that way.
- **Do not invent numbers.** No coverage percentage, no per-claim citation
  index, no fee or duration that did not come from the server payload. If a
  field is absent, omit the element.
- Preserve the accessibility work: `:focus-visible` rings, `aria-expanded` on
  every disclosure, `aria-live` on the conversation, 44px minimum touch targets,
  `prefers-reduced-motion` handling, RTL logical properties.

## Optional follow-up (ask before doing it)

The evidence rail can show the literal sentence each number came from. That
needs one new field per source in the `/chat` response:

```python
{"url": ..., "authority": ..., "service": ..., "title": ..., "quote": "..."}
```

`script.js` already renders `src.quote || src.snippet` when present. If you add
it, take the quote verbatim from the retrieved document — never from the model.
