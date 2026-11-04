"""
Turn TikTok's live HTTP-FLV into one continuous, crash-safe MPEG-TS file.

Why this exists: TikTok's FLV connection regularly drops and has to be
re-opened (most often when a guest joins/leaves and the stream's resolution
changes). Each new connection is a brand-new FLV stream - its own file
header, its own metadata and timestamps that restart from an unrelated
value. Appending those bytes to one file (what the recorder used to do)
leaves a file whose second half most tools silently discard: ffmpeg stops
at the first splice, so everything after the first reconnect is lost.

`FlvNormalizer` fixes the splice in pure Python, without decoding anything:
it drops the repeated file headers/metadata and rewrites every tag's
timestamp so the whole recording is one monotonic timeline starting at 0.
A mid-stream resolution change is then just a new codec sequence header
inside a single valid FLV stream.

`TsRemuxer` pipes that normalized FLV through `ffmpeg -c copy` into MPEG-TS.
That's a container rewrite only (no re-encode, near-zero CPU). TS carries
the codec parameters in-band before every keyframe, so resolution changes
survive and the file stays playable even if the process dies mid-write.
Turning it into a browser-friendly MP4 is a separate, offline step (done
on a GPU, outside this server).
"""

import struct
import subprocess
import threading
from collections import deque

from utils.logger_manager import logger

_TAG_AUDIO = 8
_TAG_VIDEO = 9
_TAG_SCRIPT = 18

# Gap inserted between the last tag of one connection and the first tag of
# the next (and across any in-stream timestamp jump) - roughly one frame.
_SPLICE_GAP_MS = 40
# Audio/video tags are interleaved, so timestamps legitimately wobble a bit
# backwards; anything beyond these limits is treated as a discontinuity.
_MAX_BACKWARD_MS = 1000
_MAX_FORWARD_MS = 5_000


def _is_sequence_header(tag_type, data):
    """True for AVC/HEVC decoder config or AAC AudioSpecificConfig tags."""
    if len(data) < 2:
        return False
    if tag_type == _TAG_VIDEO:
        if data[0] & 0x80:  # Enhanced RTMP/FLV: packet type in low nibble
            return data[0] & 0x0F == 0
        return data[0] & 0x0F in (7, 12) and data[1] == 0  # AVC / HEVC
    if tag_type == _TAG_AUDIO:
        return data[0] >> 4 == 10 and data[1] == 0  # AAC
    return False


class FlvNormalizer:
    """
    Incremental FLV re-writer. Feed it raw chunks with `feed()`; call
    `new_connection()` before feeding bytes from a re-opened stream.
    """

    def __init__(self):
        self._buffer = bytearray()
        self._expect_header = True
        self._first_connection = True
        self._connection_started = False
        self._offset = 0
        self._last_out_ts = None
        self._header = None
        self._sequence_headers = {}  # tag type -> last config tag bytes
        self._pending = []  # config tags seen before a connection's 1st frame

    def new_connection(self):
        # Leftover partial tag from the dropped connection is unusable.
        self._buffer.clear()
        self._pending.clear()
        self._expect_header = True
        self._connection_started = False
        if self._header is not None:
            self._first_connection = False

    def feed(self, chunk):
        self._buffer.extend(chunk)
        out = bytearray()

        if self._expect_header:
            if len(self._buffer) < 9:
                return bytes(out)
            if self._buffer[:3] != b"FLV":
                raise ValueError("Stream is not FLV (bad signature)")
            header_len = struct.unpack(">I", self._buffer[5:9])[0]
            if len(self._buffer) < header_len + 4:
                return bytes(out)
            header = bytes(self._buffer[: header_len + 4])
            del self._buffer[: header_len + 4]
            self._expect_header = False
            if self._header is None:
                self._header = header
                out += header

        while len(self._buffer) >= 11:
            if self._buffer[:3] == b"FLV":
                # A second file header where a tag should start: bytes from
                # a re-opened stream appended without `new_connection()`
                # (e.g. an old raw file from before this normalizer existed).
                # "F" (0x46) masks to tag type 6, which is never a real tag.
                header_len = struct.unpack(">I", self._buffer[5:9])[0]
                if len(self._buffer) < header_len + 4:
                    break
                del self._buffer[: header_len + 4]
                self._pending.clear()
                self._connection_started = False
                self._first_connection = False
                continue

            tag_type = self._buffer[0] & 0x1F
            data_size = int.from_bytes(self._buffer[1:4], "big")
            total = 11 + data_size + 4
            if len(self._buffer) < total:
                break

            tag = self._buffer[:total]
            del self._buffer[:total]

            if tag_type == _TAG_SCRIPT and not self._first_connection:
                continue  # stale onMetaData from a re-opened stream
            if tag_type not in (_TAG_AUDIO, _TAG_VIDEO, _TAG_SCRIPT):
                continue

            out += self._rewrite(tag_type, tag)

        return bytes(out)

    def _rewrite(self, tag_type, tag):
        data = tag[11:-4]
        is_config = tag_type == _TAG_SCRIPT or _is_sequence_header(tag_type, data)
        if tag_type != _TAG_SCRIPT and is_config:
            self._sequence_headers[tag_type] = bytes(tag)

        if is_config:
            # Config/metadata tags carry no media time of their own (TikTok
            # often stamps them 0 while frames are at some large value), so
            # they never move the timeline - they're pinned to the frame
            # that follows them.
            if not self._connection_started:
                self._pending.append(tag)
                return b""
            return self._stamp(tag, self._last_out_ts or 0)

        ts = int.from_bytes(tag[4:7], "big") | (tag[7] << 24)
        if not self._connection_started:
            self._connection_started = True
            if self._last_out_ts is None:
                self._offset = -ts
            else:
                self._offset = self._last_out_ts + _SPLICE_GAP_MS - ts

        out_ts = ts + self._offset
        if self._last_out_ts is not None and (
            out_ts < self._last_out_ts - _MAX_BACKWARD_MS
            or out_ts > self._last_out_ts + _MAX_FORWARD_MS
        ):
            self._offset = self._last_out_ts + _SPLICE_GAP_MS - ts
            out_ts = ts + self._offset
        out_ts = max(out_ts, 0)
        self._last_out_ts = (
            out_ts if self._last_out_ts is None else max(self._last_out_ts, out_ts)
        )

        out = bytearray()
        for pending in self._pending:
            out += self._stamp(pending, out_ts)
        self._pending.clear()
        out += self._stamp(tag, out_ts)
        return bytes(out)

    @staticmethod
    def _stamp(tag, out_ts):
        tag = bytearray(tag)
        tag[4:7] = (out_ts & 0xFFFFFF).to_bytes(3, "big")
        tag[7] = (out_ts >> 24) & 0xFF
        return bytes(tag)

    def restart_prefix(self):
        """
        Bytes that make a valid FLV stream start for a fresh consumer joining
        mid-recording: the file header plus the current codec configs.
        """
        if self._header is None:
            return b""
        prefix = bytearray(self._header)
        for tag_type in (_TAG_VIDEO, _TAG_AUDIO):
            if tag_type in self._sequence_headers:
                prefix += self._sequence_headers[tag_type]
        return bytes(prefix)


class TsRemuxer:
    """
    Writes a normalized FLV byte stream into an MPEG-TS file via an
    `ffmpeg -c copy` child process. If ffmpeg dies mid-recording, a new one
    is started and keeps appending to the same file (TS segments can simply
    be concatenated), so a hiccup never ends the recording.
    """

    def __init__(self, output_path, ffmpeg_path=None, max_restarts=20):
        self.output_path = output_path
        self._ffmpeg = ffmpeg_path or "ffmpeg"
        self._max_restarts = max_restarts
        self._restarts = 0
        self._normalizer = FlvNormalizer()
        self._proc = None
        self._out_file = open(output_path, "ab")
        self._stderr_tail = deque(maxlen=20)

    def _start(self):
        self._proc = subprocess.Popen(
            [
                self._ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "+genpts+discardcorrupt",
                "-f",
                "flv",
                "-i",
                "pipe:0",
                "-map",
                "0",
                "-c",
                "copy",
                "-copyts",
                "-f",
                "mpegts",
                "-mpegts_flags",
                "+resend_headers",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=self._out_file,
            stderr=subprocess.PIPE,
        )
        threading.Thread(
            target=self._drain_stderr, args=(self._proc,), daemon=True
        ).start()

    def _drain_stderr(self, proc):
        for line in proc.stderr:
            text = line.decode(errors="replace").rstrip()
            if text:
                self._stderr_tail.append(text)

    def new_connection(self):
        self._normalizer.new_connection()

    def write(self, chunk):
        data = self._normalizer.feed(chunk)
        if not data:
            return
        if self._proc is None:
            self._start()
        try:
            self._proc.stdin.write(data)
        except (BrokenPipeError, OSError):
            self._restart(data)

    def _restart(self, data):
        self._proc.wait()
        detail = " | ".join(self._stderr_tail) or f"exit code {self._proc.returncode}"
        if self._restarts >= self._max_restarts:
            raise RuntimeError(f"ffmpeg remuxer keeps failing: {detail}")
        self._restarts += 1
        logger.warning(f"ffmpeg remuxer died ({detail}); restarting it")
        self._start()
        prefix = self._normalizer.restart_prefix()
        self._proc.stdin.write(prefix + data)

    def close(self):
        try:
            if self._proc is not None:
                try:
                    self._proc.stdin.close()
                except OSError:
                    pass
                self._proc.wait()
                if self._proc.returncode not in (0, None):
                    logger.warning(
                        "ffmpeg remuxer exited with "
                        f"{self._proc.returncode}: {' | '.join(self._stderr_tail)}"
                    )
        finally:
            self._out_file.close()
