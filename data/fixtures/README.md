# data/fixtures — INVENTED CONTENT

These files are **not** corpus. They are stand-in Arabic service documents written
during development so retrieval could be built and tested before the real corpus
existed.

Every file here carries `fixture: true` in its front-matter, and
`scripts/build_index.py` **refuses** to write fixture content into the real index at
`data/index/`.

```bash
python scripts/build_index.py --fixtures   # -> data/fixtures/index/
```

## Rules

1. Never copy a fixture into `data/source_docs/`.
2. Never quote a number measured against fixtures. D2's gate thresholds and D7's
   accuracy figure must come from the real corpus — those numbers get said out loud to
   judges.
3. Fixtures are for wiring, not for evidence.
