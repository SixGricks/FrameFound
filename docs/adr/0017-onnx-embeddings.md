# ADR-0017: ONNX Runtime for visual embeddings

- Status: Accepted
- Date: 2026-07-29

## Context
Visual search needs CLIP-style embeddings. The roadmap flagged this as the
highest-risk milestone because the deployment host is a 2010-era Westmere Xeon
with **no AVX**, and official PyTorch wheels require AVX — they fail to load
outright. The assumption was that M5 would have to wait for new hardware.

That assumption conflated *one runtime* with *the capability*.

## Decision
Run CLIP through **ONNX Runtime** with pre-exported models
(`Xenova/clip-vit-base-patch32`), behind an `EmbeddingProvider` interface.

ONNX Runtime's kernel library dispatches at runtime from CPUID rather than
requiring a baseline instruction set at load time — the same property that let
faster-whisper (CTranslate2) work on this host. Measured on the actual
production CPU:

| Operation | Time |
|---|---|
| Image → 512-d vector | 288 ms |
| Text query → vector | 35 ms |

At that rate a ~9,000-asset library indexes in roughly 70 minutes of
background work, and search stays interactive permanently, because embedding
is a one-time cost per asset and querying is an indexed lookup.

Vectors are L2-normalised and stored in pgvector with an HNSW index using
cosine ops. HNSW over IVFFlat: no training step, and accuracy holds as rows
arrive continuously from background processing rather than in one bulk load.

## Alternatives considered
- **PyTorch + OpenCLIP**: the reference implementation, but will not load
  without AVX. Viable only after a host upgrade, and it buys nothing that
  ONNX does not already provide at this scale.
- **Building PyTorch from source without AVX**: days of work, a bespoke
  toolchain to maintain forever, for a slower result than ONNX.
- **A cloud embedding API**: violates the local-first principle and would put
  the contents of a private archive on someone else's servers.
- **ViT-L/14 instead of B/32**: better matches, roughly 4× slower (~5 hours
  for this library). The provider interface makes this a config change, so
  the decision can be revisited empirically on real footage.

## Consequences
- M5 shipped on existing hardware; the roadmap's "wait for a CPU upgrade"
  risk is retired.
- The embedding dimension (512) is baked into the migration. Changing model
  families means a new column plus a re-embed pass, which is exactly the
  versioned-model path described in docs/data-model.md.
- A GPU, when it arrives, is a drop-in acceleration (`onnxruntime-gpu`) rather
  than a rewrite.
