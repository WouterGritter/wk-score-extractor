"""Grab still frames from a (possibly live) stream via ffmpeg.

`grab_frame` opens ffmpeg per call (for the once-a-minute IDLE poll).
`StreamGrabber` keeps one ffmpeg tuned and serves the latest frame on demand
(for ACTIVE fast polling), so the tune-in is paid once, not every cycle.
"""
from __future__ import annotations

import glob
import io
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time

from PIL import Image

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _hwaccel_args(hwaccel: bool) -> list[str]:
    """Pre-input flags to decode on the GPU (NVDEC) instead of the CPU.

    `-hwaccel cuda` offloads HEVC decode to the GPU and auto-downloads frames to
    system memory, so the fps filter + PNG encode downstream are unchanged. Needs
    an ffmpeg built with cuda/cuvid AND the container's NVIDIA `video` driver
    capability (compute-only won't expose libnvcuvid). If unsupported, ffmpeg
    errors on open — hence this is opt-in, not the default.
    """
    return ["-hwaccel", "cuda"] if hwaccel else []


class CaptureError(RuntimeError):
    pass


def _read_exact(stream, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _read_png(stream) -> bytes | None:
    """Read exactly one PNG (signature .. IEND) from an image2pipe stream."""
    sig = _read_exact(stream, 8)
    if sig != _PNG_SIG:
        return None
    data = bytearray(sig)
    while True:
        header = _read_exact(stream, 8)          # length(4) + type(4)
        if header is None:
            return None
        data += header
        length = int.from_bytes(header[:4], "big")
        body = _read_exact(stream, length + 4)   # data + CRC
        if body is None:
            return None
        data += body
        if header[4:8] == b"IEND":
            return bytes(data)


def grab_frame(url: str, seek: float = 2.0, timeout: float = 40.0,
               loglevel: str = "error", probesize: int = 500000,
               hwaccel: bool = False) -> Image.Image:
    """Return one RGB frame from `url` (~4 s on the live feed).

    `-analyzeduration 0` cuts ffmpeg's stream analysis (open ~9 s -> ~2 s); `seek`
    then discards the first couple of error-concealed (gray) frames until the
    HEVC decoder syncs (2.0 s is reliable; below ~1.5 s yields gray frames).
    """
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    cmd = ["ffmpeg", "-nostdin", "-loglevel", loglevel, "-y",
           "-probesize", str(probesize), "-analyzeduration", "0",
           "-fflags", "nobuffer", *_hwaccel_args(hwaccel), "-i", url]
    if seek > 0:
        cmd += ["-ss", str(seek)]
    cmd += ["-frames:v", "1", "-q:v", "2", path]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
        if proc.returncode != 0 or not os.path.getsize(path):
            raise CaptureError(
                f"ffmpeg failed (rc={proc.returncode}): "
                f"{proc.stderr.decode(errors='replace').strip()[-300:]}")
        img = Image.open(path)
        img.load()  # before the temp file is removed
        return img.convert("RGB")
    except subprocess.TimeoutExpired:
        raise CaptureError(f"capture timed out after {timeout}s")
    finally:
        if os.path.exists(path):
            os.remove(path)


def grab_frames_at(path: str, t: float, count: int = 3, fps: float = 2.0,
                   timeout: float = 40.0, loglevel: str = "error") -> list[Image.Image]:
    """Return up to `count` RGB frames sampled near `t` seconds into a local file.

    Uses **input seeking** (`-ss` before `-i`): ffmpeg jumps to the keyframe near
    `t` and decodes only a small chunk, so this is fast even on a 30 GB `.ts`
    (unlike `grab_frame`'s post-input `-ss`, which decodes from the start). The
    `fps` filter spaces the frames out (e.g. fps=2 -> ~0.5 s apart) so a probe
    spans a short window rather than three near-identical native-rate frames —
    that spread is what lets an offline probe tolerate a single replay/blip frame.

    Input seeking lands on a clean keyframe, so no gray-frame skip is needed here
    (that was a live fast-open artifact, not a seek-into-file one).
    """
    tmpdir = tempfile.mkdtemp(prefix="probe-")
    pattern = os.path.join(tmpdir, "f_%03d.png")
    cmd = ["ffmpeg", "-nostdin", "-loglevel", loglevel, "-y",
           "-ss", f"{max(0.0, t):.3f}", "-i", path,
           "-vf", f"fps={fps}", "-frames:v", str(count), "-q:v", "2", pattern]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
        if proc.returncode != 0:
            raise CaptureError(
                f"ffmpeg failed (rc={proc.returncode}): "
                f"{proc.stderr.decode(errors='replace').strip()[-300:]}")
        frames = []
        for p in sorted(glob.glob(os.path.join(tmpdir, "f_*.png"))):
            img = Image.open(p)
            img.load()
            frames.append(img.convert("RGB"))
        return frames
    except subprocess.TimeoutExpired:
        raise CaptureError(f"frame probe timed out after {timeout}s")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def probe_duration(path: str, timeout: float = 30.0) -> float:
    """Container duration in seconds via ffprobe (0.0 if unavailable)."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=nokey=1:noprint_wrappers=1", path]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
        return float(proc.stdout.decode().strip())
    except (subprocess.SubprocessError, ValueError):
        return 0.0


class StreamGrabber:
    """Keeps one ffmpeg tuned and serves the latest decoded frame on demand.

    A background thread drains complete PNG frames from ffmpeg's pipe (keeping
    only the newest), so `read()` never blocks on decode, never returns a
    half-written frame, and never lags behind real time.

    With `buffer_dir` set, the *same* ffmpeg also stream-copies the source into a
    rolling ring of keyframe-aligned `.ts` segments — one tuner, no extra decode
    (the copy path re-muxes packets), so a goal replay can be cut from the last
    N seconds on demand (`snapshot`). Put `buffer_dir` on a tmpfs to keep the
    ring entirely in RAM.
    """

    def __init__(self, url: str, fps: int = 4, loglevel: str = "error",
                 probesize: int = 500000, hwaccel: bool = False,
                 buffer_dir: str | None = None, buffer_seconds: float = 270.0,
                 segment_time: float = 2.0):
        self.url = url
        self.fps = fps
        self.loglevel = loglevel
        self.probesize = probesize
        self.hwaccel = hwaccel
        self.buffer_dir = buffer_dir
        self.segment_time = segment_time
        # Ring length in filenames; segment_wrap reuses them cyclically so the
        # footprint is bounded to ~buffer_seconds of stream regardless of runtime.
        self._segment_wrap = max(2, math.ceil(buffer_seconds / segment_time))
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._cond = threading.Condition()
        self._latest: bytes | None = None
        self._seq = 0

    def start(self) -> "StreamGrabber":
        cmd = ["ffmpeg", "-nostdin", "-loglevel", self.loglevel,
               "-probesize", str(self.probesize), "-analyzeduration", "0",
               "-fflags", "nobuffer", *_hwaccel_args(self.hwaccel), "-i", self.url,
               # OCR output: audio-less, low-fps PNG frames on stdout.
               "-map", "0:v", "-an", "-vf", f"fps={self.fps}",
               "-f", "image2pipe", "-c:v", "png", "pipe:1"]
        if self.buffer_dir:
            self._prepare_buffer_dir()
            cmd += [
                # Replay ring: stream-copy (no re-encode) video+audio into a
                # cyclic set of .ts segments. reset_timestamps keeps each segment
                # self-contained so they concat cleanly later.
                "-map", "0:v", "-map", "0:a?", "-c", "copy",
                "-f", "segment", "-segment_time", str(self.segment_time),
                "-segment_wrap", str(self._segment_wrap),
                "-reset_timestamps", "1",
                os.path.join(self.buffer_dir, "seg_%05d.ts"),
            ]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, bufsize=0)
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        return self

    def _prepare_buffer_dir(self) -> None:
        """Create the ring dir and drop any leftovers from a prior session."""
        os.makedirs(self.buffer_dir, exist_ok=True)
        for old in glob.glob(os.path.join(self.buffer_dir, "seg_*.ts")):
            try:
                os.remove(old)
            except OSError:
                pass
        for old in glob.glob(os.path.join(self.buffer_dir, "clip-*")):
            shutil.rmtree(old, ignore_errors=True)

    def snapshot(self, seconds: float) -> tuple[str, str] | None:
        """Copy the most recent ~`seconds` of buffered segments to a fresh temp
        dir and write an ffmpeg concat list for them. Returns (concat_file,
        temp_dir) — caller re-encodes from the list and removes temp_dir — or
        None if the ring is empty / no buffer configured.

        Segments are copied (not referenced) so the recorder's segment_wrap can
        keep overwriting the ring without corrupting an in-flight clip. The
        newest segment (still being written) is skipped.
        """
        if not self.buffer_dir:
            return None
        entries = []
        for p in glob.glob(os.path.join(self.buffer_dir, "seg_*.ts")):
            try:
                entries.append((os.path.getmtime(p), p))
            except OSError:
                pass
        entries.sort()
        segs = [p for _, p in entries][:-1]  # drop the in-progress newest
        if not segs:
            return None
        k = max(1, math.ceil(seconds / self.segment_time))
        chosen = segs[-k:]
        # Work dir lives inside buffer_dir so the copy + re-encode stay on the
        # same tmpfs as the ring (all in RAM); seg_*.ts globbing ignores it.
        tmpdir = tempfile.mkdtemp(prefix="clip-", dir=self.buffer_dir)
        listed = []
        for i, src in enumerate(chosen):
            dst = os.path.join(tmpdir, f"part_{i:05d}.ts")
            try:
                shutil.copy(src, dst)
            except OSError:
                continue  # segment got recycled mid-copy; skip it
            listed.append(dst)
        if not listed:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None
        concat = os.path.join(tmpdir, "concat.txt")
        with open(concat, "w") as f:
            for p in listed:
                f.write(f"file '{p}'\n")
        return concat, tmpdir

    def _reader(self) -> None:
        stream = self._proc.stdout
        while self._running:
            png = _read_png(stream)
            if png is None:
                break
            with self._cond:
                self._latest = png
                self._seq += 1
                self._cond.notify_all()
        with self._cond:
            self._running = False
            self._cond.notify_all()

    def read(self, timeout: float = 15.0, after_seq: int = 0):
        """Return (image, seq): the latest frame newer than `after_seq`,
        waiting up to `timeout`. Raises CaptureError if none arrives."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while not (self._latest is not None and self._seq > after_seq):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._running:
                    raise CaptureError("no fresh frame from stream")
                self._cond.wait(remaining)
            png, seq = self._latest, self._seq
        img = Image.open(io.BytesIO(png))
        img.load()
        return img.convert("RGB"), seq

    def alive(self) -> bool:
        return (self._running and self._proc is not None
                and self._proc.poll() is None)

    def stop(self) -> None:
        self._running = False
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None


if __name__ == "__main__":
    import sys
    out = sys.argv[2] if len(sys.argv) > 2 else "frame.png"
    grab_frame(sys.argv[1]).save(out)
    print(f"saved {out}")
