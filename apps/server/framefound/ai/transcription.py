"""Speech transcription provider interface + local faster-whisper backend.

The provider contract is deliberately tiny: a path in, timestamped segments
out. Diarization, word alignment, and cloud backends can implement the same
protocol later without touching callers.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import structlog

from framefound.config import get_settings

log = structlog.get_logger()

# Hallucination guards, tuned against real music-bed audio.
MIN_AVG_LOGPROB = -1.0
MAX_NO_SPEECH_PROB = 0.6
# Whisper's own "this decode failed" signal: text that compresses this well
# may be a repetition loop ("the the the ..."). It is only a hint, never a
# verdict on its own — see keep_segment.
MAX_COMPRESSION_RATIO = 2.4

# Phrases Whisper is known to invent over room tone and music beds — it
# learned them from the closing credits of subtitled video. Nobody on an
# auction floor or a property walk-through says them, so a cue that is
# nothing else is dropped outright, however confident the model claims to be.
HALLUCINATED_PHRASES = frozenset(
    {
        "thank you for watching",
        "thanks for watching",
        "thank you so much for watching",
        "please subscribe",
        "like and subscribe",
        "please like and subscribe",
        "subtitles by the amara org community",
        "transcription by castingwords",
        "subtitles by",
    }
)


def _normalised(text: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text.lower()).split())


def keep_segment(
    text: str,
    avg_logprob: float | None,
    no_speech_prob: float | None,
    compression_ratio: float | None = None,
) -> bool:
    """Whether a decoded cue is speech someone actually said.

    Silence is Whisper's rule, not either signal alone: a cue is treated as
    invented only when it is *both* probably-not-speech and low-confidence.
    Dropping on either one threw away real dialogue — an auctioneer over a
    generator, a seller with an accent — which is exactly the talking the
    catalogue exists to find.

    Repetition follows the same rule. faster-whisper measures the
    compression ratio over the whole ~30 s decoded window, keeps the window
    when no temperature brings it under 2.4, and stamps that ratio on every
    cue cut from it. An auctioneer's chant ("fifty, fifty, now fifty-five,
    fifty-five...") compresses past 2.4 however clearly it was heard, so
    dropping on the ratio alone erased the bid calling — and any ordinary
    speech sharing its window. A loop the model was also unsure of goes.
    """
    clean = text.strip()
    if len(clean) <= 1:
        return False
    if _normalised(clean) in HALLUCINATED_PHRASES:
        return False
    unsure = avg_logprob is not None and avg_logprob <= MIN_AVG_LOGPROB
    if unsure and compression_ratio is not None and compression_ratio > MAX_COMPRESSION_RATIO:
        return False
    silent = no_speech_prob is not None and no_speech_prob >= MAX_NO_SPEECH_PROB
    return not (unsure and silent)


@dataclass(frozen=True)
class SpeechSegment:
    start_s: float
    end_s: float
    text: str
    confidence: float | None = None


@dataclass(frozen=True)
class TranscriptionResult:
    language: str
    language_probability: float
    duration_s: float
    model_name: str
    segments: list[SpeechSegment] = field(default_factory=list)


class TranscriptionProvider(Protocol):
    def transcribe(self, source: Path) -> TranscriptionResult: ...


class TranscriptionUnavailable(RuntimeError):
    """The configured provider cannot run on this host/installation."""


class FasterWhisperProvider:
    """Local CTranslate2-based Whisper. Model weights download on first use
    into the models volume; int8 on CPU, float16 on CUDA."""

    def __init__(self, model_size: str, device: str, download_root: str = "/models") -> None:
        self._model_size = model_size
        self._device = device
        self._download_root = download_root
        self._model: object | None = None

    def _load(self) -> object:
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as err:
                raise TranscriptionUnavailable(
                    "Speech recognition is not installed on this server"
                ) from err
            compute = "float16" if self._device == "cuda" else "int8"
            log.info("transcription.loading_model", model=self._model_size, device=self._device)
            self._model = WhisperModel(
                self._model_size,
                device=self._device,
                compute_type=compute,
                download_root=self._download_root,
                # Leave cores for the API/UI on shared hosts; raise with hardware.
                cpu_threads=get_settings().whisper_threads,
                num_workers=1,
            )
        return self._model

    def transcribe(self, source: Path) -> TranscriptionResult:
        model = self._load()
        segments_iter, info = model.transcribe(  # type: ignore[attr-defined]
            str(source),
            # Beam search is the accuracy Whisper is benchmarked at; on a CPU
            # it costs several times the greedy decode, so it is a GPU luxury.
            beam_size=5 if self._device == "cuda" else 1,
            # Whisper invents dialogue from music beds and room tone. Silero
            # VAD skips non-speech audio outright, which both removes the
            # hallucinations and shortens the work (seen on stock music in a
            # real library).
            vad_filter=True,
            condition_on_previous_text=False,  # stops repetition loops
        )
        segments = [
            SpeechSegment(
                start_s=float(seg.start),
                end_s=float(seg.end),
                text=seg.text.strip(),
                confidence=float(seg.avg_logprob) if seg.avg_logprob is not None else None,
            )
            for seg in segments_iter
            if keep_segment(
                seg.text,
                seg.avg_logprob,
                seg.no_speech_prob,
                getattr(seg, "compression_ratio", None),
            )
        ]
        return TranscriptionResult(
            language=info.language,
            language_probability=float(info.language_probability),
            duration_s=float(info.duration),
            model_name=f"faster-whisper/{self._model_size}",
            segments=segments,
        )


_provider: TranscriptionProvider | None = None


def get_transcription_provider() -> TranscriptionProvider:
    """Provider factory (cached per process). Swappable in tests."""
    global _provider
    if _provider is None:
        settings = get_settings()
        device = "cuda" if settings.compute == "cuda" else "cpu"
        _provider = FasterWhisperProvider(settings.whisper_model, device)
    return _provider
