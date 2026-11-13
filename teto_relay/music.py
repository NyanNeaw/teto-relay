"""The music track: the backing track of a cover, kept with its take.

In Studio a song (wav, mp3, flac or ogg) can be put on the music row. It is
stored beside the take it belongs to - or, before there is a take, in the
``_next`` folder of the takes directory, from where the next take copies it:

* ``music.<ext>``       - the file as it was given;
* ``music.json``        - its name, length and format;
* ``music-peaks.json``  - its waveform, for the panel to draw.

While a take records, the music plays on the Hear myself device (`Backing`),
so it can be sung along to, and the take's clock starts at the music's first
sample: a line sung on the beat lands on the beat. The music always starts at
0:00, and it is never mixed into ``teto.wav``; ``mix.wav`` (her singing over
the music) is made when it is asked for (`mix_wav`).

Everything that reads the music - the sing-along, the panel's player and the
mix - decodes it here, with one decoder, so all three agree to the sample
(mp3 decoders do not all skip the same padding at the start).
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import threading
import time
from pathlib import Path

import numpy as np

from .errors import TetoRelayError

log = logging.getLogger(__name__)

#: What can be put on the music row.
FORMATS = (".wav", ".mp3", ".flac", ".ogg")
MAX_UPLOAD = 300_000_000      # bytes; a ten-minute wav is about 100 MB
MAX_SECONDS = 30 * 60.0       # a song, not an album
#: Where music waits before there is a take to keep it with.
NEXT = "_next"
#: How finely the waveform is kept: points a second, and points in all.
PEAKS_PER_SECOND = 50
MAX_PEAKS = 8000
NOT_AUDIO = "That file isn't music Teto Relay can read. Use a .wav, .mp3, .flac or .ogg."


class MusicError(TetoRelayError):
    """The music cannot be played while recording. The message says why."""


# ------------------------------------------------------------------ the files
def home(root: Path, take_id: str) -> Path | None:
    """The folder that holds the music for `take_id`: a take's own, or (for
    "next") the place it waits for the next take. None: no such take."""
    from .takes import take_folder

    if take_id == "next":
        return Path(root) / NEXT
    return take_folder(root, take_id)


def music_file(folder: Path) -> Path | None:
    for ext in FORMATS:
        path = Path(folder) / f"music{ext}"
        if path.is_file():
            return path
    return None


def info(folder: Path) -> dict | None:
    """What is on the music row: {name, seconds, rate, channels, file, stamp}.
    `stamp` changes when the file does, so the panel knows to fetch it again."""
    path = music_file(folder) if folder is not None and Path(folder).is_dir() else None
    if path is None:
        return None
    try:
        data = json.loads((Path(folder) / "music.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if data.get("file") != path.name or "seconds" not in data:
        data = _describe(path, path.name)
    data["stamp"] = path.stat().st_mtime_ns // 1_000_000
    return data


def _describe(path: Path, name: str) -> dict:
    import soundfile as sf

    found = sf.info(str(path))
    return {"name": name, "file": path.name, "seconds": round(float(found.duration), 3),
            "rate": int(found.samplerate), "channels": int(found.channels)}


def store(data: bytes, filename: str, folder: Path) -> dict:
    """Keep an uploaded song as `folder`'s music, in place of any it had.

    It is checked first, by opening it: a file that is not audio is refused
    with a reason and nothing changes. Raises ValueError with something the
    user can act on; returns the music's `info`.
    """
    import soundfile as sf

    name = Path(str(filename).replace("\\", "/")).name.strip() or "music"
    ext = Path(name).suffix.lower()
    if ext not in FORMATS:
        raise ValueError(NOT_AUDIO)
    if not data:
        raise ValueError("That file is empty.")
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"That file is {len(data) / 1e6:.0f} MB; music can be up to {MAX_UPLOAD / 1e6:.0f} MB.")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    trial = folder / f".adding-music{ext}"
    try:
        trial.write_bytes(data)
        try:
            found = _describe(trial, name)
            with sf.SoundFile(str(trial)) as audio:
                heard = len(audio.read(min(len(audio), 4096), dtype="float32"))
        except Exception:  # noqa: BLE001 - whatever the decoder says, it is not music to us
            raise ValueError(NOT_AUDIO) from None
        if not heard or found["seconds"] <= 0:
            raise ValueError("That file has no sound in it.")
        if found["seconds"] > MAX_SECONDS:
            raise ValueError(f"That is {found['seconds'] / 60:.0f} minutes long; "
                             f"music can be up to {MAX_SECONDS / 60:.0f}.")
        remove(folder)
        path = folder / f"music{ext}"
        trial.replace(path)
        found["file"] = path.name
        (folder / "music.json").write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
        _forget(folder)
        log.info("Music added: %s (%.1fs) in %s", name, found["seconds"], folder.name)
        return info(folder)
    finally:
        trial.unlink(missing_ok=True)


def save_audio(data: bytes, filename: str, stem: Path) -> dict:
    """Keep an audio file dropped on the Studio's playlist (a layer: a beat,
    a DAW bounce, a backing vocal) as `stem` plus its own extension, checked
    the way a song is (`store`). Returns {name, file, seconds, rate,
    channels}, `file` being the kept file's name. Raises ValueError with
    something the user can act on, and keeps nothing then."""
    import soundfile as sf

    name = Path(str(filename).replace("\\", "/")).name.strip() or "audio"
    ext = Path(name).suffix.lower()
    if ext not in FORMATS:
        raise ValueError(NOT_AUDIO)
    if not data:
        raise ValueError("That file is empty.")
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"That file is {len(data) / 1e6:.0f} MB; audio can be up to {MAX_UPLOAD / 1e6:.0f} MB.")
    stem = Path(stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    path = stem.with_name(stem.name + ext)
    trial = stem.with_name(f".adding-{stem.name}{ext}")
    try:
        trial.write_bytes(data)
        try:
            found = _describe(trial, name)
            with sf.SoundFile(str(trial)) as audio:
                heard = len(audio.read(min(len(audio), 4096), dtype="float32"))
        except Exception:  # noqa: BLE001 - whatever the decoder says, it is not audio to us
            raise ValueError(NOT_AUDIO) from None
        if not heard or found["seconds"] <= 0:
            raise ValueError("That file has no sound in it.")
        if found["seconds"] > MAX_SECONDS:
            raise ValueError(f"That is {found['seconds'] / 60:.0f} minutes long; "
                             f"audio can be up to {MAX_SECONDS / 60:.0f}.")
        trial.replace(path)
        found["file"] = path.name
        return found
    finally:
        trial.unlink(missing_ok=True)


def remove(folder: Path) -> bool:
    """Take the music off; True if there was any."""
    folder = Path(folder)
    had = music_file(folder) is not None
    for path in [folder / f"music{ext}" for ext in FORMATS] + [folder / "music.json", folder / "music-peaks.json"]:
        path.unlink(missing_ok=True)
    _forget(folder)
    return had


def copy_into(source: Path, take: Path) -> bool:
    """A new take keeps the music it was recorded over. Never raises: the
    take matters more than its music."""
    try:
        path = music_file(source)
        if path is None or Path(source).resolve() == Path(take).resolve():
            return False
        for name in (path.name, "music.json", "music-peaks.json"):
            if (Path(source) / name).is_file():
                shutil.copy2(Path(source) / name, Path(take) / name)
        return True
    except OSError:
        log.warning("could not keep the music with the take", exc_info=True)
        return False


# ------------------------------------------------------------------ the sound
_DECODED: dict[str, tuple[tuple, np.ndarray, int]] = {}
_DECODE_LOCK = threading.Lock()


def _forget(folder: Path) -> None:
    _DECODED.pop(str(Path(folder).resolve()), None)


def load(folder: Path) -> tuple[np.ndarray, int]:
    """The music as float32 frames (always 2-D: frames x channels) and its
    rate. The last one read is kept, since recording, playing and mixing a
    take all want the same song."""
    import soundfile as sf

    path = music_file(folder)
    if path is None:
        raise ValueError("There is no music here.")
    stat = path.stat()
    key, stamp = str(Path(folder).resolve()), (path.name, stat.st_mtime_ns, stat.st_size)
    with _DECODE_LOCK:
        kept = _DECODED.get(key)
        if kept is not None and kept[0] == stamp:
            return kept[1], kept[2]
        audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
        _DECODED.clear()
        _DECODED[key] = (stamp, audio, int(rate))
        return audio, int(rate)


def peaks_of(audio: np.ndarray, count: int) -> list[int]:
    """The waveform in `count` points, each 0-100: the loudest sample in its
    stretch of the song, against the loudest in the whole of it."""
    audio = np.abs(np.asarray(audio, dtype=np.float32))
    if audio.ndim > 1:
        audio = audio.max(axis=1)
    count = max(1, min(int(count), len(audio))) if len(audio) else 0
    if not count:
        return []
    edges = np.linspace(0, len(audio), count + 1).astype(np.int64)
    tops = np.maximum.reduceat(audio, edges[:-1])
    loudest = float(tops.max())
    if loudest <= 0:
        return [0] * count
    return [int(v) for v in np.round(tops / loudest * 100)]


def peaks(folder: Path) -> list[int]:
    """The music's waveform (`peaks_of`), worked out once and kept beside it."""
    path = music_file(folder)
    if path is None:
        return []
    stat = path.stat()
    stamp = [path.name, stat.st_mtime_ns, stat.st_size]
    cache = Path(folder) / "music-peaks.json"
    try:
        kept = json.loads(cache.read_text(encoding="utf-8"))
        if kept.get("of") == stamp:
            return kept["peaks"]
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    audio, rate = load(folder)
    seconds = len(audio) / rate
    out = peaks_of(audio, min(MAX_PEAKS, max(1, int(round(seconds * PEAKS_PER_SECOND)))))
    try:
        cache.write_text(json.dumps({"of": stamp, "peaks": out}), encoding="utf-8")
    except OSError:
        pass
    return out


def wav_bytes(folder: Path) -> bytes:
    """The music as a 16-bit .wav, for the panel's player."""
    import soundfile as sf

    audio, rate = load(folder)
    buffer = io.BytesIO()
    sf.write(buffer, audio, rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def mix_tracks(music: np.ndarray, music_rate: int, voice: np.ndarray, voice_rate: int) -> tuple[np.ndarray, int]:
    """Her singing over the music, both from 0:00, at her singing's rate.
    The music keeps its channels; she is in the middle. A sum that would
    clip is turned down, not cut off."""
    return mix_layers(voice, voice_rate, [(0.0, music, music_rate)])


def mix_layers(voice: np.ndarray, voice_rate: int, layers: list) -> tuple[np.ndarray, int]:
    """Her singing with other sounds laid over it, at her singing's rate:
    `layers` are (seconds, audio, rate), each from its time (the music from
    0:00, an audio clip from where it sits on the playlist). Stereo if any
    layer is; she and mono layers are in the middle. A sum that would clip
    is turned down, not cut off."""
    from .playback import _resample

    placed = []
    channels = 1
    for at, audio, rate in layers:
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim == 1:
            audio = audio[:, None]
        audio = _resample(audio, int(rate), int(voice_rate))
        channels = max(channels, min(2, audio.shape[1]))
        placed.append((max(0, int(round(float(at) * int(voice_rate)))), audio))
    voice = np.asarray(voice, dtype=np.float32)
    if voice.ndim > 1:
        voice = voice.mean(axis=1)
    total = max([len(voice)] + [start + len(a) for start, a in placed])
    out = np.zeros((total, channels), dtype=np.float64)
    for start, audio in placed:
        if audio.shape[1] != channels:
            audio = np.repeat(audio.mean(axis=1, keepdims=True), channels, axis=1)
        out[start:start + len(audio)] += audio[:, :channels]
    out[:len(voice)] += voice[:, None]
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 1.0:
        out /= peak
    return out.astype(np.float32), int(voice_rate)


def mix_wav(take: Path) -> bytes:
    """mix.wav: the take's teto.wav over its music and the audio clips laid
    on its playlist. Made when asked for, not kept: a song's worth is tens of
    megabytes."""
    import soundfile as sf

    from .takes import audio_layers

    take = Path(take)
    layers = audio_layers(take)
    if music_file(take) is not None:
        music, music_rate = load(take)
        layers.insert(0, (0.0, music, music_rate))
    if not layers:
        raise ValueError("This take has no music or audio to mix with.")
    voice, voice_rate = sf.read(str(take / "teto.wav"), dtype="float32", always_2d=False)
    mixed, rate = mix_layers(voice, voice_rate, layers)
    buffer = io.BytesIO()
    sf.write(buffer, mixed, rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


# ------------------------------------------------- playing it while recording
def first_sample_time(now: float, current: float, dac: float, latency: float) -> float:
    """When (time.monotonic) the block being filled is heard. `current` and
    `dac` are the stream's own clock now and at the block's first sample, as
    PortAudio gives them to the callback; some drivers give neither, and
    then the stream's stated `latency` is the best there is."""
    ahead = dac - current
    if not (current > 0 and 0 <= ahead < 2.0):
        ahead = latency if 0 <= latency < 2.0 else 0.0
    return now + ahead


def refuse_cable(name: str) -> None:
    """The music is for your ears only: never the virtual cable, where it
    would go out as your microphone (and round into the recording)."""
    if "cable input" in (name or "").lower() or "voicemeeter" in (name or "").lower():
        raise MusicError(
            f"The music would play into {name.strip()}, not your headphones. Pick your headphones "
            "under All settings, Setup, Hear myself on."
        )


class Backing:
    """The music, playing on one output device from its first sample.

    `start()` returns the moment (time.monotonic) that first sample is heard,
    as near as the driver can say: the take's clock starts there, so what is
    sung on the beat is on the beat. What the driver cannot say - most of a
    Bluetooth headset's delay, for one - is what Studio's delay setting is for.
    """

    def __init__(self, audio: np.ndarray, rate: int, device: int | None, open_stream=None):
        self.audio = np.ascontiguousarray(audio, dtype=np.float32)
        self.rate = int(rate)
        self.device = device
        self.position = 0            # frames handed to the device so far
        self.started_at: float | None = None
        self._latency = 0.0
        self._stream = None
        self._first = threading.Event()
        self._open = open_stream

    def fill(self, out: np.ndarray, frames: int) -> bool:
        """The next `frames` of the music into `out`; False once it is over."""
        piece = self.audio[self.position:self.position + frames]
        out[:len(piece)] = piece
        out[len(piece):] = 0
        self.position += frames
        return self.position < len(self.audio)

    def _callback(self, out, frames, time_info, status) -> None:
        if self.started_at is None:
            self.started_at = first_sample_time(
                time.monotonic(), float(getattr(time_info, "currentTime", 0.0) or 0.0),
                float(getattr(time_info, "outputBufferDacTime", 0.0) or 0.0), self._latency)
            self._first.set()
        if not self.fill(out, frames):
            from .devices import sd

            raise sd().CallbackStop

    def start(self, wait: float = 3.0) -> float:
        from .devices import sd

        opener = self._open or sd().OutputStream
        self._stream = opener(samplerate=self.rate, channels=self.audio.shape[1], dtype="float32",
                              device=self.device, callback=self._callback)
        try:
            self._latency = float(getattr(self._stream, "latency", 0.0) or 0.0)
        except (TypeError, ValueError):
            self._latency = 0.0
        self._stream.start()
        if not self._first.wait(wait) or self.started_at is None:
            self.stop()
            raise MusicError("The music did not start playing. Check the Hear myself device.")
        return self.started_at

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001 - it is going away either way
            log.debug("closing the music stream failed", exc_info=True)


def backing_for(folder: Path, cfg) -> Backing:
    """The take's music, ready to play on the Hear myself device: decoded,
    and fitted to the rate and channels that device takes. Raises MusicError
    (or ValueError, with no music there) rather than play somewhere wrong."""
    from .devices import sd
    from .playback import _device_format, _fit, monitor_device

    audio, rate = load(folder)
    device = monitor_device(cfg)
    if device is None:
        raise MusicError("There is nothing to play the music on. Plug in your headphones.")
    try:
        name = str(sd().query_devices(device)["name"])
    except Exception:  # noqa: BLE001 - a device that will not say its name will not open either
        name = ""
    refuse_cable(name)
    target_rate, target_channels = _device_format(device)
    audio, rate = _fit(audio, rate, target_rate, target_channels)
    log.info("Music for the take: %.1fs on %s (%d Hz)", len(audio) / rate, name or f"device {device}", rate)
    return Backing(audio, rate, device)
