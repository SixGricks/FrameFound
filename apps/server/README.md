# mediahub server

One Python package, multiple entrypoints (see ADR-0001):

| Entrypoint | Command | Role |
|---|---|---|
| API | `uvicorn mediahub.main:app` | HTTP API + auth + media serving |
| Worker | `celery -A mediahub.worker worker -Q default,media` | metadata, thumbnails, proxies (TODO m1) |
| AI worker | `celery -A mediahub.worker worker -Q transcribe,vision` | transcription, embeddings (TODO m4/m5) |
| Scanner | `python -m mediahub.scanner` | watch + reconcile libraries (TODO m2) |
| Scheduler | `celery -A mediahub.worker beat` | periodic jobs (TODO m1) |
| DDNS | `python -m mediahub.ddns` | optional DDNS sidecar (TODO m7) |

## Develop

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
ruff check . && mypy mediahub
uvicorn mediahub.main:app --reload   # http://localhost:8000/api/docs
```
