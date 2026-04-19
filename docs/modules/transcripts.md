# transcripts

Transcript parsing and cross-host sync. `cai_lib/parse.py` is the
deterministic signal extractor that feeds `cai-analyze`;
`cai_lib/transcript_sync.py` fans JSONL session transcripts between
long-lived workers and the audit host.

## Entry points
- `cai_lib/parse.py` — Deterministic signal extractor from JSONL transcripts.
- `cai_lib/transcript_sync.py` — Cross-host transcript sync.
