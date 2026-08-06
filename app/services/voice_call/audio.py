import audioop
import io
import wave


def downsample_to_16k_mono(pcm: bytes, in_rate: int, in_channels: int) -> bytes:
    """Convert raw 16-bit PCM (Discord voice: 48kHz stereo) to 16kHz mono PCM."""
    if in_channels == 2:
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    converted, _ = audioop.ratecv(pcm, 2, 1, in_rate, 16000, None)
    return converted


def pcm_to_wav_bytes(pcm: bytes, sample_rate: int, channels: int = 1, sample_width: int = 2) -> bytes:
    """Wrap raw PCM in an in-memory WAV container for upload to the STT server."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()
