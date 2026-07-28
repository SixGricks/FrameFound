# Test fixture media (planned — M2)

A tiny, legally redistributable media corpus for automated tests:

- images: JPEG/PNG/TIFF/WebP/HEIC with EXIF+GPS variants (self-produced, CC0)
- video: short MP4 (H.264), MOV, MKV, HEVC clip with a spoken sentence for
  transcription tests; one file per codec family we claim to support
- audio: WAV/MP3/FLAC with known speech content
- adversarial: zero-byte file, truncated MP4, wrong-extension file, filename
  with quotes/unicode/emoji, 10,000-file directory generator script

Rules: every file's provenance and license recorded in `MANIFEST.md`
(TODO m2); nothing scraped from the internet without a verified CC0/CC-BY
license; total size kept under ~50 MB so clones stay fast (larger corpus
fetched by CI on demand).
