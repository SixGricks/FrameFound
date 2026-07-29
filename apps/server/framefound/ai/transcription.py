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
            beam_size=1,
            # Whisper invents dialogue from music beds and room tone. Silero
            # VAD skips non-speech audio outright, which both removes the
            # hallucinations and shortens the work (seen on stock music in a
            # real library).
            vad_filter=True,
            condition_on_previous_text=False,  # stops repetition loops
        )
        segments = [
            segment
            for segment in (
                SpeechSegment(
                    start_s=float(seg.start),
                    end_s=float(seg.end),
                    text=seg.text.strip(),
                    confidence=float(seg.avg_logprob) if seg.avg_logprob is not None else None,
                )
                for seg in segments_iter
                if seg.text.strip()
                # Low average log-probability and high no-speech probability
                # are the two signals that a cue is invented rather than heard.
                and (seg.avg_logprob is None or seg.avg_logprob > MIN_AVG_LOGPROB)
                and (seg.no_speech_prob is None or seg.no_speech_prob < MAX_NO_SPEECH_PROB)
            )
            if len(segment.text) > 1
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
