# data/source_docs — the Daleel corpus

Curated Arabic markdown, one file per government service.

**Format is a contract:** see [../../docs/CORPUS_SPEC.md](../../docs/CORPUS_SPEC.md).
A file that does not follow it fails the build with a named error.

```bash
python scripts/build_index.py
```

This directory is intentionally empty of service files until the team's curated
content lands. Fixtures used for development live in `data/fixtures/` and must never
be copied here.
