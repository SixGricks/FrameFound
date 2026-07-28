# License Compatibility Checklist

Core project: **Apache-2.0** (permissive, patent grant, business-friendly).
This inventory must be revisited before every release; CI will gain an
automated license scan (M1).

## Principles

- Source-code licenses and **model-weight licenses are tracked separately**.
- Nothing copyleft (GPL) may be *linked into* our distributed images in a way
  that contaminates the core. Invoking GPL **binaries as subprocesses**
  (FFmpeg, ExifTool) is fine and is our pattern.
- Models with non-commercial or restricted licenses are **never bundled**;
  at most offered as user-initiated external downloads with license display.
- The UI must display attribution/licenses for shipped components (M6).

## Inventory (initial)

| Component | License | Type | Status / notes |
|---|---|---|---|
| FastAPI, Pydantic, SQLAlchemy, Celery, structlog | MIT/BSD/MIT/BSD/Apache-2.0 | code | ✅ compatible |
| PostgreSQL, pgvector | PostgreSQL, PostgreSQL | code | ✅ |
| Valkey 8 (Redis-compatible) | BSD-3 | code | ✅ adopted over Redis 7.4+ relicensing (ADR-0016) |
| Next.js, React, Tailwind, shadcn/ui | MIT | code | ✅ |
| Caddy | Apache-2.0 | code | ✅ |
| FFmpeg | LGPL-2.1+ core; **GPL if built with x264/x265** | code (subprocess) | ⚠️ we call the binary, don't link — OK, but the *image we distribute* includes it: document GPL components in NOTICE; consider LGPL build + openh264 fallback review (M3) |
| ExifTool | Perl Artistic/GPL dual | code (subprocess) | ✅ subprocess use |
| Pillow, OpenCV-headless, PySceneDetect | MIT-CMU / Apache-2.0 / BSD | code | ✅ |
| faster-whisper (CTranslate2) | MIT | code | ✅ |
| **Whisper weights** (OpenAI) | MIT | model | ✅ redistributable, but download at runtime anyway |
| OpenCLIP code | MIT-style | code | ✅ |
| **LAION OpenCLIP weights** | MIT-style (verify per checkpoint) | model | ⚠️ verify exact checkpoint license at M5 |
| SigLIP weights (if adopted) | Apache-2.0 | model | ✅ candidate |
| PaddleOCR / Tesseract (OCR candidates) | Apache-2.0 / Apache-2.0 | code+model | ✅ verify Paddle model files at M5 |
| Face models (post-MVP) | varies — many are **non-commercial** | model | 🚫 gate hard; external download only |
| Adobe UXP/CEP SDK | Adobe ToS | code | review at M9; panel may need separate licensing terms |
| Icon set (Lucide), fonts (Inter) | ISC / OFL-1.1 | assets | ✅ OFL requires attribution — include in NOTICE |
| Base Docker images (python-slim, node-alpine, caddy, pgvector, redis) | various permissive + OS packages | distribution | generate SBOM per image in CI (syft) from M1 |

## Actions

- [ ] M1: add `pip-licenses` / `license-checker` + syft SBOM to CI
- [x] M1: decide Redis-vs-Valkey pin — Valkey adopted (ADR-0016)
- [ ] M3: document FFmpeg build flags used in our image; NOTICE file
- [ ] M5: verify chosen CLIP checkpoint + OCR model licenses
- [ ] M6: in-app attribution/licenses screen
