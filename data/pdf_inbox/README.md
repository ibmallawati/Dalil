# PDF inbox

Drop `.pdf` files here, then run:

```powershell
.\venv\Scripts\python.exe scripts/ocr_inbox.py
```

Each `<name>.pdf` becomes `data/pdf_ocr/<name>.ocr.txt`.

Useful flags:

| Flag | Effect |
|---|---|
| `--force` | re-OCR a file whose `.ocr.txt` already exists |
| `scripts/ocr_inbox.py path/to/one.pdf` | just that file |

## This produces text, not knowledge

**Running this does not teach the chatbot anything.** It stops at readable
text so you can judge a document before importing it.

Adding that text to the corpus is a separate step that is not built yet, and
it has real decisions attached — chiefly that OCR recovers no usable URLs
from these ministry documents, so citations have to be textual, and that a
ministry the registry lists as `not_covered` is refused *before* retrieval
runs, so its services would be indexed but unreachable.

See [../../docs/PDF_INTAKE_SPEC.md](../../docs/PDF_INTAKE_SPEC.md).

## Every page is scanned, even digital ones

Some PDFs already contain real text, so OCR looks unnecessary for them. It
runs anyway.

On the ministry PDFs, the embedded Arabic used U+064B in place of the space
character. It was long enough to look perfectly fine to any "does this page
have text?" check, and it would have fused every word into one once tashkeel
stripping ran — with no error to notice.

Scanning everything means there is no shortcut that can silently pick the
wrong text. It is slower, and a page that had good digital text may pick up
an OCR typo it would not otherwise have — a visible flaw, which is the
better trade against an invisible one.

Anything that needs correcting is corrected once, afterwards, in the
cleaner.

PDFs themselves are not committed — only their `.ocr.txt` output is.
