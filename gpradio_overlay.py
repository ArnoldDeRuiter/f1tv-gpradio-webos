#!/usr/bin/env python3
"""Background daemon: shows a toggle-able Grand Prix Radio widget over F1TV's
page and plays its audio, so this app can play F1TV video and GPRadio audio
at the same time -- something two separate webOS apps can't do (switching
away from an app stops its audio/video, a hard platform limit, not
something this gets around).

Real audio playback and the now-playing fetch both happen HERE, in this
plain root process -- not inside the injected page JS. F1TV's own CSP
(media-src, connect-src) only allows loading from formula1.com/bitmovin/
akamaized etc; confirmed live that both an injected <audio src> pointed at
Grand Prix Radio's stream and a page-side fetch() to grandprixradio.nl get
silently blocked by it. A plain root process has no such restriction.

Audio itself goes through `gst-launch-1.0 ... ! alsasink device=media`, NOT
the platform's own lgaudiosink/audiosink GStreamer element -- that one
needs a resource "index" negotiated through LG's proprietary (and, on this
TV, completely undocumented/unreachable) media resource manager, confirmed
live via dmesg's `lgao_feed: index is invalid` no matter what index value
was set. `alsasink device=media` instead routes through this TV's real
underlying audio stack, PulseAudio -- confirmed by reading the installed
`plx-native` app's own `/etc/asound.conf` (ALSA-over-PulseAudio with named
stream roles: media, tts, alerts, ringtones, etc; "media" is the sensible
one for this), present identically outside any jail too, so a plain root
process gets the same routing. Confirmed audible live.

Same CDP-injection architecture as loginfill.py/scrollfix.py otherwise, but
this daemon does NOT exit once it has injected something -- it needs to
keep running for as long as the app stays open, both to manage the audio
subprocess and because `Page.addScriptToEvaluateOnNewDocument` only keeps
re-applying the widget across any real top-level navigation inside the app
(e.g. f1tv.formula1.com -> account.formula1.com for login) for as long as
this CDP WebSocket connection stays open.

The widget itself is appended as a sibling of F1TV's own React root `<div>`,
not inside it -- React only manages its own subtree, so this avoids the
widget being wiped out by F1TV's own re-renders.

Blue/red give a real DVR-style rewind/forward (see RadioProcess.seek) to
sync the radio commentary against the video by hand -- not just a release-
rate delay, which turned out to be audibly undetectable (confirmed live:
it just smoothly kept playing forward with no perceptible effect at all).
"""

import json
import os
import socket
import subprocess
import threading
import time
import urllib.request

CDP_HOST = "127.0.0.1"
CDP_PORT = 9998
APP_DESCRIPTION = "nl.arnolderuiter.f1tvgpradio"
POLL_INTERVAL_SECONDS = 3

STREAM_URL = "https://eu-player-redirect.streamtheworld.com/api/livestream-redirect/GRAND_PRIX_RADIOAAC.aac"
NOWPLAYING_URL = "https://grandprixradio.nl/soundtracks/grand-prix-radio.json"
NOWPLAYING_POLL_SECONDS = 15

# ~128kbps AAC -> ~16KB/s. Approximate (real bitrate can drift slightly),
# but this only has to be good enough for a coarse rewind step, not exact.
# Confirmed live via the feed debug log: read rate tracks this closely.
AUDIO_BYTES_PER_SECOND = 16000
NUDGE_SMALL_SECONDS = 0.5
NUDGE_LARGE_SECONDS = 5.0

# How far back rewinding can reach. Generous on purpose (plenty of room for
# repeated taps/holds) -- at ~16KB/s this is only ~1.9MB, trivial to hold.
MAX_RETAINED_SECONDS = 120
MAX_RETAINED_BYTES = MAX_RETAINED_SECONDS * AUDIO_BYTES_PER_SECOND

# Workaround, not a fix: audio reliably goes silent after roughly a minute
# of play, confirmed live and repeatedly -- every layer we can inspect
# (this process, gst-launch's own stderr, a `level` meter on the decoded
# audio, PulseAudio's sink-input, PulseAudio's sink, the ALSA hardware
# mixer, the raw kernel PCM clock, com.webos.audio, and webOS's own
# structured notification log) reports perfectly healthy at the exact
# moment it goes silent -- see TODO.md for the full investigation. A full
# restart of the gst-launch subprocess reliably recovers it, so this
# restarts proactively before that point rather than waiting to notice and
# press green. The rewind/seek position survives the restart (see
# start()'s _preserved_lag_seconds handling), so this is just a brief,
# periodic re-connection, not a loss of sync.
AUTO_RESTART_INTERVAL_SECONDS = 30

GST_LOG_PATH = "/tmp/f1tvgpradio-gst.log"
MONITOR_INTERVAL_SECONDS = 2

GST_FEED_CMD = [
    "gst-launch-1.0",
    "-m",  # print bus messages (incl. the `level` element's RMS/peak) to stdout
    "fdsrc",
    "fd=0",
    "!",
    # Without this, decodebin/alsasink maintain their own multi-second
    # internal buffer -- confirmed live: nudging the delay target updated
    # the widget correctly but had no audible effect at all, since our
    # upstream pacing changes were just absorbed into that existing slack
    # instead of reaching the actual output. Capping it here forces our
    # feed-rate changes to propagate to real playback within ~0.3s instead.
    "queue",
    "max-size-time=300000000",  # 0.3s, in nanoseconds
    "max-size-buffers=0",
    "max-size-bytes=0",
    "!",
    "decodebin",
    "!",
    "audioconvert",
    "!",
    # Reports periodic RMS/peak of the actual decoded audio -- proves
    # whether real (non-silent) audio is reaching this point in the
    # pipeline at all, independent of whether the hardware PCM device
    # downstream looks healthy (confirmed live: it can look perfectly
    # healthy -- hw_ptr advancing at exactly 44100Hz -- while Captain hears
    # nothing, meaning either silence or corrupted samples are what's
    # actually reaching it).
    "level",
    "!",
    "alsasink",
    "device=media",
]

# Standard webOS/HbbTV color-button keyCodes. Deliberately not calling
# preventDefault/stopPropagation on these in the injected JS -- if F1TV's
# own page also binds one of them for something, that should keep working
# too; this is a judgment call to revisit if live testing shows a conflict.
KEYCODE_RED = 403  # nudge sync earlier (tap -0.5s, hold -5s)
KEYCODE_GREEN = 404  # toggle play/pause
KEYCODE_YELLOW = 405  # toggle widget visibility (OLED burn-in guard)
KEYCODE_BLUE = 406  # nudge sync later (tap +0.5s, hold +5s)

PLAY_TOGGLE_MARKER = "__gpradioTogglePlay__"
NUDGE_MARKER_PREFIX = "__gpradioNudge__"


def _handshake(sock, host, port, path):
    import base64

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = [
        "GET %s HTTP/1.1" % path,
        "Host: %s:%d" % (host, port),
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: %s" % key,
        "Sec-WebSocket-Version: 13",
        "",
        "",
    ]
    sock.sendall("\r\n".join(lines).encode("ascii"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("connection closed during handshake")
        buf += chunk
    header = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    status_line = header.split("\r\n", 1)[0]
    if " 101 " not in (" " + status_line + " "):
        raise OSError("unexpected handshake response: %s" % status_line)


def _send_frame(sock, payload):
    data = payload.encode("utf-8")
    header = bytearray([0x80 | 0x1])
    length = len(data)
    mask = os.urandom(4)
    if length <= 125:
        header.append(0x80 | length)
    elif length <= 0xFFFF:
        header.append(0x80 | 126)
        header += length.to_bytes(2, "big")
    else:
        header.append(0x80 | 127)
        header += length.to_bytes(8, "big")
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(bytes(header) + masked)


def _recv_frame(sock):
    def recv_exact(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("connection closed")
            buf += chunk
        return buf

    first2 = recv_exact(2)
    opcode = first2[0] & 0x0F
    length = first2[1] & 0x7F
    if length == 126:
        length = int.from_bytes(recv_exact(2), "big")
    elif length == 127:
        length = int.from_bytes(recv_exact(8), "big")
    payload = recv_exact(length) if length else b""
    return opcode, payload


def build_overlay_js():
    # Token replacement rather than %-formatting: the CSS below has literal
    # '%' characters (e.g. border-radius:50%), which %-formatting mis-parses
    # as format specifiers ("not enough arguments for format string").
    template = """
(function(){
  if (window.__gpradioOverlayInstalled) return;
  window.__gpradioOverlayInstalled = true;

  var KEYCODE_RED = __KEYCODE_RED__;
  var KEYCODE_GREEN = __KEYCODE_GREEN__;
  var KEYCODE_YELLOW = __KEYCODE_YELLOW__;
  var KEYCODE_BLUE = __KEYCODE_BLUE__;
  var PLAY_TOGGLE_MARKER = __PLAY_TOGGLE_MARKER__;
  var NUDGE_MARKER_PREFIX = __NUDGE_MARKER_PREFIX__;
  var NUDGE_SMALL_SECONDS = __NUDGE_SMALL_SECONDS__;
  var NUDGE_LARGE_SECONDS = __NUDGE_LARGE_SECONDS__;

  function build() {
    var wrap = document.createElement('div');
    wrap.id = 'gpradio-overlay';
    wrap.style.cssText = 'position:fixed;right:24px;bottom:24px;z-index:999999;'
      + 'background:rgba(10,20,40,0.85);color:#fff;font-family:sans-serif;'
      + 'font-size:18px;padding:10px 16px;border-radius:8px;display:flex;'
      + 'align-items:center;gap:10px;pointer-events:none;';

    var dot = document.createElement('span');
    dot.id = 'gpradio-dot';
    dot.style.cssText = 'width:10px;height:10px;border-radius:50%;background:#888;flex:none;';

    var label = document.createElement('span');
    label.id = 'gpradio-label';
    label.textContent = 'Grand Prix Radio';

    var offset = document.createElement('span');
    offset.id = 'gpradio-offset';
    offset.style.cssText = 'opacity:0.7;font-size:14px;';

    wrap.appendChild(dot);
    wrap.appendChild(label);
    wrap.appendChild(offset);
    document.body.appendChild(wrap);

    return { wrap: wrap, dot: dot, label: label, offset: offset };
  }

  // Real playback, the now-playing fetch, and the sync-offset buffering all
  // happen in the daemon (plain root process, not subject to F1TV's CSP) --
  // this page-side code only shows state and relays key presses back to it
  // via a console marker, the same signalling technique loginfill.py uses.
  window.__gpradioEls = build();
  var visible = true;
  var holdFired = {};

  document.addEventListener('keydown', function(e){
    if (e.keyCode === KEYCODE_GREEN) {
      console.log(PLAY_TOGGLE_MARKER);
    } else if (e.keyCode === KEYCODE_YELLOW) {
      visible = !visible;
      window.__gpradioEls.wrap.style.display = visible ? 'flex' : 'none';
    } else if (e.keyCode === KEYCODE_RED || e.keyCode === KEYCODE_BLUE) {
      var sign = (e.keyCode === KEYCODE_BLUE) ? 1 : -1;
      if (!e.repeat) {
        console.log(NUDGE_MARKER_PREFIX + JSON.stringify({seconds: sign * NUDGE_SMALL_SECONDS}));
      } else if (!holdFired[e.keyCode]) {
        // e.repeat fires repeatedly for as long as a key is physically
        // held (native browser key-repeat) -- only fire the bigger hold
        // jump once per hold, not once per repeat tick.
        holdFired[e.keyCode] = true;
        console.log(NUDGE_MARKER_PREFIX + JSON.stringify({seconds: sign * NUDGE_LARGE_SECONDS}));
      }
    }
  });

  document.addEventListener('keyup', function(e){
    holdFired[e.keyCode] = false;
  });
})();
"""
    return (
        template.replace("__KEYCODE_RED__", str(KEYCODE_RED))
        .replace("__KEYCODE_GREEN__", str(KEYCODE_GREEN))
        .replace("__KEYCODE_YELLOW__", str(KEYCODE_YELLOW))
        .replace("__KEYCODE_BLUE__", str(KEYCODE_BLUE))
        .replace("__PLAY_TOGGLE_MARKER__", json.dumps(PLAY_TOGGLE_MARKER))
        .replace("__NUDGE_MARKER_PREFIX__", json.dumps(NUDGE_MARKER_PREFIX))
        .replace("__NUDGE_SMALL_SECONDS__", str(NUDGE_SMALL_SECONDS))
        .replace("__NUDGE_LARGE_SECONDS__", str(NUDGE_LARGE_SECONDS))
    )


def _find_target():
    while True:
        try:
            with urllib.request.urlopen(
                "http://%s:%d/json" % (CDP_HOST, CDP_PORT), timeout=5
            ) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
        except OSError:
            targets = []
        for target in targets:
            if target.get("description") == APP_DESCRIPTION:
                return target
        time.sleep(POLL_INTERVAL_SECONDS)


def _call(sock, msg_id, method, params=None):
    _send_frame(sock, json.dumps({"id": msg_id, "method": method, "params": params or {}}))


def _set_indicator_js(is_playing):
    color = "#2ecc71" if is_playing else "#888"
    return "window.__gpradioEls && (window.__gpradioEls.dot.style.background = %s);" % json.dumps(color)


def _set_label_js(text):
    return "window.__gpradioEls && (window.__gpradioEls.label.textContent = %s);" % json.dumps(text)


def _set_offset_js(delay_seconds):
    text = "" if delay_seconds <= 0 else "+%.1fs" % delay_seconds
    return "window.__gpradioEls && (window.__gpradioEls.offset.textContent = %s);" % json.dumps(text)


def _pactl_sink_input_summary():
    """One-line summary of our gst-launch client's PulseAudio sink-input --
    which sink it's on, whether it's corked (paused by Pulse itself) or
    muted, roughly how far behind its buffer is. Returns a diagnostic
    string, never raises -- this is purely for debugging, must never take
    the daemon down if pactl itself has a bad day."""
    try:
        out = subprocess.run(
            ["pactl", "list", "sink-inputs"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return "pactl_error=%s" % exc
    # Find the block whose "application.process.binary" is gst-launch-1.0 --
    # assumes only one such client exists, true for this app's own use.
    blocks = out.split("Sink Input #")
    for block in blocks:
        if "gst-launch-1.0" not in block:
            continue
        fields = {}
        for line in block.splitlines():
            line = line.strip()
            for key in ("Sink:", "Corked:", "Mute:", "Buffer Latency:", "Sink Latency:"):
                if line.startswith(key):
                    fields[key.rstrip(":")] = line[len(key):].strip()
        return "sink_input=%s" % fields
    return "sink_input=NOT FOUND (gst-launch has no active pulse stream right now)"


def _load_and_memory_summary():
    """One-line load average + free memory, never raises."""
    try:
        with open("/proc/loadavg") as f:
            load1 = f.read().split()[0]
    except OSError:
        load1 = "?"
    free_kb = "?"
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    free_kb = line.split()[1]
                    break
    except OSError:
        pass
    return "load1=%s mem_available_kb=%s" % (load1, free_kb)


def _fetch_now_playing():
    try:
        with urllib.request.urlopen(NOWPLAYING_URL, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        attrs = (data.get("data") or {}).get("attributes") or {}
        return attrs.get("full_title") or "Grand Prix Radio"
    except (OSError, ValueError, AttributeError):
        return "Grand Prix Radio"


def _console_message_value(payload):
    """Returns the first console.log argument's string value, or None if
    this isn't a console API call message at all."""
    try:
        msg = json.loads(payload.decode("utf-8"))
    except ValueError:
        return None
    if msg.get("method") != "Runtime.consoleAPICalled":
        return None
    args = msg.get("params", {}).get("args", [])
    if not args:
        return None
    return args[0].get("value")


class RadioProcess:
    """Owns the background audio pipeline and a feeder thread that fetches
    the live stream itself (rather than letting gst-launch's souphttpsrc
    fetch it directly), so sync-offset nudges can be implemented as real
    control over how much audio sits buffered before reaching the player:
    increasing the target delay holds more back (audio drifts later),
    decreasing it releases the backlog, bottoming out at zero once there's
    nothing left to release -- you can't make a live stream play earlier
    than live itself. That's the only honest, controllable way to do this
    against a live radio stream with just gst-launch's CLI: there's no
    PyGObject/Gst Python bindings on this TV to control a persistent
    pipeline's properties directly, confirmed live (`ModuleNotFoundError:
    No module named 'gi'`)."""

    def __init__(self):
        self._proc = None
        self._stop_event = None
        self._lock = threading.Lock()
        # Absolute byte positions in the conceptual (never-reset) downloaded
        # stream. read_pos is where playback currently is; total_downloaded
        # is the live edge; oldest_retained_pos is the earliest point still
        # in the retained buffer (can't rewind past it). read_pos ==
        # total_downloaded means "live, no rewind". Rewinding jumps read_pos
        # backward -- immediately, not gradually -- so it actually replays
        # already-downloaded audio, which is what makes this feel like a
        # real rewind instead of the earlier "gradually fall behind, never
        # actually audible" delay design.
        self._read_pos = 0
        self._total_downloaded = 0
        self._oldest_retained_pos = 0
        self._preserved_lag_seconds = 0.0

    def is_playing(self):
        return self._proc is not None and self._proc.poll() is None

    def toggle(self):
        if self.is_playing():
            self.stop()
            return False
        self.start()
        return True

    def current_lag_seconds(self):
        with self._lock:
            return (self._total_downloaded - self._read_pos) / AUDIO_BYTES_PER_SECOND

    def start(self):
        with self._lock:
            # Preserve how far behind live we currently are -- a restart
            # (via the green-button toggle) is the only reliable recovery
            # from the still-unexplained stall where every diagnostic layer
            # we can inspect reports healthy, yet no audio reaches the
            # speaker. A restart tears down the retained buffer entirely
            # (fresh connection, fresh download from scratch), so the exact
            # bytes can't carry over, but the SECONDS-behind-live value can
            # be re-applied once the fresh feed has downloaded enough to
            # support it -- see _feed_loop's initial_lag_applied handling.
            self._preserved_lag_seconds = (self._total_downloaded - self._read_pos) / AUDIO_BYTES_PER_SECOND
            self._read_pos = 0
            self._total_downloaded = 0
            self._oldest_retained_pos = 0
        self._stop_event = threading.Event()
        # gst-launch's own stdout/stderr were previously thrown away
        # (DEVNULL) -- captured to a file now since a stutter/stop needs to
        # show up as a real decoder/sink error here, not just silence.
        gst_log = open(GST_LOG_PATH, "ab", buffering=0)
        self._proc = subprocess.Popen(
            GST_FEED_CMD,
            stdin=subprocess.PIPE,
            stdout=gst_log,
            stderr=gst_log,
        )
        threading.Thread(
            target=self._feed_loop, args=(self._proc, self._stop_event), daemon=True
        ).start()
        threading.Thread(
            target=self._monitor_loop, args=(self._proc, self._stop_event), daemon=True
        ).start()

    def stop(self):
        if self._stop_event is not None:
            self._stop_event.set()
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            self._proc.terminate()
            self._proc = None

    def _monitor_loop(self, proc, stop_event):
        """Independent of the write-pacing loop on purpose -- polls
        PulseAudio's own view of our stream (corked/muted/which sink) plus
        process liveness and system load, so a stutter/stop shows up here
        even if the feed loop itself looks perfectly healthy throughout
        (confirmed live: it did, during a real stutter -- the fault must be
        downstream of our write() calls, in gst/PulseAudio/ALSA, not in the
        feeder itself). Also proactively restarts the subprocess on a timer
        -- see AUTO_RESTART_INTERVAL_SECONDS."""
        started_at = time.monotonic()
        while not stop_event.wait(MONITOR_INTERVAL_SECONDS):
            alive = proc.poll() is None
            sink_input_info = _pactl_sink_input_summary()
            load_mem = _load_and_memory_summary()
            print(
                "gpradio-overlay: monitor: proc_alive=%s %s %s"
                % (alive, sink_input_info, load_mem),
                flush=True,
            )
            if not alive:
                print("gpradio-overlay: monitor: process died, stopping monitor", flush=True)
                return
            if time.monotonic() - started_at >= AUTO_RESTART_INTERVAL_SECONDS:
                print("gpradio-overlay: monitor: proactive restart (workaround for unexplained stall)", flush=True)
                # stop() first -- without it, the old gst-launch process
                # (and its feed thread) would be orphaned rather than torn
                # down, leaving two processes writing to the same sink.
                self.stop()
                self.start()
                return

    def seek(self, seconds_delta):
        """Positive seconds_delta rewinds (jumps read_pos backward to replay
        already-downloaded audio); negative seeks forward (toward live).
        Jumps immediately -- clamped to what's still retained on one side
        and the live edge on the other -- rather than gradually, which is
        what actually makes this feel like a rewind instead of the earlier
        delay design's "smoothly keeps playing forward, offset does
        nothing" (confirmed live: a gradually-approached target lag doesn't
        replay anything, it just briefly withholds new content)."""
        with self._lock:
            delta_bytes = -int(seconds_delta * AUDIO_BYTES_PER_SECOND)
            new_pos = self._read_pos + delta_bytes
            new_pos = max(self._oldest_retained_pos, min(new_pos, self._total_downloaded))
            self._read_pos = new_pos
            return (self._total_downloaded - self._read_pos) / AUDIO_BYTES_PER_SECOND

    def _feed_loop(self, proc, stop_event):
        # No "Icy-MetaData: 1" request header is sent (default urllib
        # behaviour), so the server sends a clean AAC byte stream with no
        # interleaved ICY metadata chunks to worry about.
        #
        # Keeps a sliding window of the last MAX_RETAINED_SECONDS of
        # downloaded bytes (`retained`) and an absolute, independently
        # movable read position (`self._read_pos`) into the conceptual
        # (never-reset) downloaded stream -- real DVR-style rewind, not
        # just a release-rate delay. Writes are explicitly paced to
        # real-time via time.sleep() regardless of how far read_pos is
        # from the live edge, rather than relying on write() blocking once
        # downstream is "full" -- confirmed live that it doesn't block in
        # time to matter, since the OS pipe backing subprocess.PIPE has its
        # own several-seconds buffer sitting between us and gst.
        retained = bytearray()
        write_chunk_bytes = 2048
        write_interval_seconds = write_chunk_bytes / AUDIO_BYTES_PER_SECOND
        next_write_at = time.monotonic()
        last_write_completed_at = time.monotonic()
        total_read = 0
        total_written = 0
        max_write_gap_seen = 0.0
        last_debug_print = time.monotonic()
        # Gates writing until a preserved lag (from a prior run, before a
        # restart) can actually be satisfied -- see start()'s comment.
        initial_lag_applied = False
        try:
            with urllib.request.urlopen(STREAM_URL, timeout=10) as resp:
                print("gpradio-overlay: feed opened, status=%s url=%s" % (resp.status, resp.geturl()), flush=True)
                while not stop_event.is_set():
                    chunk = resp.read(4096)
                    if not chunk:
                        print("gpradio-overlay: feed got empty chunk, stream ended", flush=True)
                        break
                    total_read += len(chunk)
                    retained.extend(chunk)

                    with self._lock:
                        self._total_downloaded += len(chunk)
                        total_downloaded = self._total_downloaded
                        if not initial_lag_applied:
                            if self._preserved_lag_seconds <= 0:
                                initial_lag_applied = True
                            else:
                                wanted_lag_bytes = int(self._preserved_lag_seconds * AUDIO_BYTES_PER_SECOND)
                                if total_downloaded >= wanted_lag_bytes:
                                    self._read_pos = total_downloaded - wanted_lag_bytes
                                    initial_lag_applied = True

                    # Trim the retained window, but never past the current
                    # read position -- that would destroy audio a rewind
                    # still needs.
                    excess = len(retained) - MAX_RETAINED_BYTES
                    if excess > 0:
                        with self._lock:
                            trim = min(excess, max(0, self._read_pos - self._oldest_retained_pos))
                        if trim > 0:
                            del retained[:trim]
                            with self._lock:
                                self._oldest_retained_pos += trim

                    if not initial_lag_applied:
                        continue

                    while True:
                        with self._lock:
                            read_pos = self._read_pos
                            oldest = self._oldest_retained_pos
                            total_downloaded = self._total_downloaded
                        available = total_downloaded - read_pos
                        if available < write_chunk_bytes:
                            break
                        now = time.monotonic()
                        if now < next_write_at:
                            time.sleep(next_write_at - now)
                        # Gap since the PREVIOUS actual write, not the
                        # scheduled one -- reveals real thread-scheduling
                        # stalls (e.g. CPU contention from F1TV's own video
                        # decode) that the schedule-based `next_write_at`
                        # bookkeeping alone wouldn't surface.
                        write_gap = time.monotonic() - last_write_completed_at
                        if write_gap > max_write_gap_seen:
                            max_write_gap_seen = write_gap
                        if write_gap > write_interval_seconds * 5:
                            print(
                                "gpradio-overlay: feed WARNING: write stalled for %.2fs (expected ~%.3fs)"
                                % (write_gap, write_interval_seconds),
                                flush=True,
                            )
                        rel_start = read_pos - oldest
                        out = bytes(retained[rel_start:rel_start + write_chunk_bytes])
                        try:
                            proc.stdin.write(out)
                            proc.stdin.flush()
                            total_written += len(out)
                        except (BrokenPipeError, OSError, ValueError) as exc:
                            # ValueError ("write to closed file") happens
                            # when stop() closes stdin while this thread is
                            # mid-write -- a real race hit live during the
                            # very first proactive auto-restart, confirmed
                            # by its traceback landing in the log.
                            print("gpradio-overlay: feed write failed: %s" % exc, flush=True)
                            return
                        last_write_completed_at = time.monotonic()
                        with self._lock:
                            self._read_pos += write_chunk_bytes
                        next_write_at += write_interval_seconds

                    now = time.monotonic()
                    if now - last_debug_print >= 2:
                        with self._lock:
                            lag = (self._total_downloaded - self._read_pos) / AUDIO_BYTES_PER_SECOND
                        print(
                            "gpradio-overlay: feed debug: read=%d written=%d retained_bytes=%d "
                            "lag_seconds=%.2f max_write_gap=%.2fs"
                            % (total_read, total_written, len(retained), lag, max_write_gap_seen),
                            flush=True,
                        )
                        last_debug_print = now
        except OSError as exc:
            print("gpradio-overlay: feed urlopen/read failed: %s" % exc, flush=True)
            return


def _watch_target(target, overlay_js):
    """Injects the widget, then stays connected for as long as the app is
    open: relays green-button presses into starting/stopping the radio
    subprocess, pushes now-playing text periodically, and watches for the
    target closing to clean up the subprocess and exit."""
    target_id = target["id"]
    ws_url = target["webSocketDebuggerUrl"]
    path = ws_url.split(CDP_HOST + ":" + str(CDP_PORT), 1)[1]
    sock = socket.create_connection((CDP_HOST, CDP_PORT), timeout=10)
    radio = RadioProcess()
    try:
        _handshake(sock, CDP_HOST, CDP_PORT, path)
        _call(sock, 1, "Page.enable")
        _call(sock, 2, "Runtime.enable")
        _call(sock, 3, "Page.addScriptToEvaluateOnNewDocument", {"source": overlay_js})
        _call(sock, 4, "Runtime.evaluate", {"expression": overlay_js})
        print("gpradio-overlay: injected into target %s" % target_id, flush=True)

        sock.settimeout(2)
        next_nowplaying_fetch = 0
        while True:
            now = time.time()
            if now >= next_nowplaying_fetch:
                title = _fetch_now_playing()
                _call(sock, 5, "Runtime.evaluate", {"expression": _set_label_js(title)})
                next_nowplaying_fetch = now + NOWPLAYING_POLL_SECONDS

            try:
                opcode, payload = _recv_frame(sock)
            except socket.timeout:
                continue
            if opcode == 0x8:
                return "closed"
            if opcode != 0x1:
                continue
            value = _console_message_value(payload)
            if value == PLAY_TOGGLE_MARKER:
                now_playing = radio.toggle()
                _call(sock, 6, "Runtime.evaluate", {"expression": _set_indicator_js(now_playing)})
                # The lag survives a restart now -- refresh the widget so it
                # shows the preserved value instead of looking reset.
                _call(sock, 8, "Runtime.evaluate", {"expression": _set_offset_js(radio.current_lag_seconds())})
            elif value and value.startswith(NUDGE_MARKER_PREFIX):
                try:
                    seconds_delta = json.loads(value[len(NUDGE_MARKER_PREFIX):])["seconds"]
                except (ValueError, KeyError, TypeError):
                    continue
                new_lag = radio.seek(seconds_delta)
                _call(sock, 7, "Runtime.evaluate", {"expression": _set_offset_js(new_lag)})
    except OSError as exc:
        print("gpradio-overlay: target %s connection ended: %s" % (target_id, exc), flush=True)
        return "error"
    finally:
        radio.stop()
        try:
            sock.close()
        except OSError:
            pass


def main():
    print("gpradio-overlay: watching for %s" % APP_DESCRIPTION, flush=True)
    overlay_js = build_overlay_js()
    target = _find_target()
    reason = _watch_target(target, overlay_js)
    print("gpradio-overlay: exiting (%s)" % reason, flush=True)


if __name__ == "__main__":
    main()
