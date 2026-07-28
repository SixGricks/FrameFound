# framefound server

One Python package, multiple entrypoints (see ADR-0001):

| Entrypoint | Command | Role |
|---|---|---|
| API | `uvicorn framefound.main:app` | HTTP API + auth + media serving |
| Worker | `celery -A framefound.worker worker -Q default,media` | metadata, thumbnails, proxies (TODO m1) |
| AI worker | `celery -A framefound.worker worker -Q transcribe,vision` | transcription, embeddings (TODO m4/m5) |
| Scanner | `python -m framefound.scanner` | watch + reconcile libraries (TODO m2) |
| Scheduler | `celery -A framefound.worker beat` | periodic jobs (TODO m1) |
| DDNS | `python -m framefound.ddns` | optional DDNS sidecar (TODO m7) |

## Develop

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
ruff check . && mypy framefound
uvicorn framefound.main:app --reload   # http://localhost:8000/api/docs
```
