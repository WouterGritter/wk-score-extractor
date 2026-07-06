"""Grab still frames from a (possibly live) stream via ffmpeg.

`grab_frame` opens ffmpeg per call (for the once-a-minute IDLE poll).
`StreamGrabber` keeps one ffmpeg tuned and serves the latest frame on demand
(for ACTIVE fast polling), so the tune-in is paid once, not every cycle.
"""
from __future__ import annotations

import io
import os
import subprocess
import tempfile
import threading
import time

from PIL import Image

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


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
               loglevel: str = "error", probesize: int = 500000) -> Image.Image:
    """Return one RGB frame from `url` (~4 s on the live feed).

    `-analyzeduration 0` cuts ffmpeg's stream analysis (open ~9 s -> ~2 s); `seek`
    then discards the first couple of error-concealed (gray) frames until the
    HEVC decoder syncs (2.0 s is reliable; below ~1.5 s yields gray frames).
    """
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    cmd = ["ffmpeg", "-nostdin", "-loglevel", loglevel, "-y",
           "-probesize", str(probesize), "-analyzeduration", "0",
           "-fflags", "nobuffer", "-i", url]
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


class StreamGrabber:
    """Keeps one ffmpeg tuned and serves the latest decoded frame on demand.

    A background thread drains complete PNG frames from ffmpeg's pipe (keeping
    only the newest), so `read()` never blocks on decode, never returns a
    half-written frame, and never lags behind real time.
    """

    def __init__(self, url: str, fps: int = 4, loglevel: str = "error",
                 probesize: int = 500000):
        self.url = url
        self.fps = fps
        self.loglevel = loglevel
        self.probesize = probesize
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._cond = threading.Condition()
        self._latest: bytes | None = None
        self._seq = 0

    def start(self) -> "StreamGrabber":
        cmd = ["ffmpeg", "-nostdin", "-loglevel", self.loglevel,
               "-probesize", str(self.probesize), "-analyzeduration", "0",
               "-fflags", "nobuffer", "-i", self.url,
               "-an", "-vf", f"fps={self.fps}",
               "-f", "image2pipe", "-c:v", "png", "pipe:1"]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, bufsize=0)
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        return self

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
