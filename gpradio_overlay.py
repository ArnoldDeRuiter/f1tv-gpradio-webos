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
# but this only has to be good enough for a coarse sync nudge, not exact.
AUDIO_BYTES_PER_SECOND = 16000
NUDGE_SMALL_SECONDS = 0.5
NUDGE_LARGE_SECONDS = 5.0

GST_FEED_CMD = [
    "gst-launch-1.0",
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
        self._delay_seconds = 0.0

    def is_playing(self):
        return self._proc is not None and self._proc.poll() is None

    def toggle(self):
        if self.is_playing():
            self.stop()
            return False
        self.start()
        return True

    def start(self):
        self._delay_seconds = 0.0
        self._stop_event = threading.Event()
        self._proc = subprocess.Popen(
            GST_FEED_CMD,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        threading.Thread(
            target=self._feed_loop, args=(self._proc, self._stop_event), daemon=True
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

    def nudge(self, seconds_delta):
        with self._lock:
            self._delay_seconds = max(0.0, self._delay_seconds + seconds_delta)
            return self._delay_seconds

    def _feed_loop(self, proc, stop_event):
        # No "Icy-MetaData: 1" request header is sent (default urllib
        # behaviour), so the server sends a clean AAC byte stream with no
        # interleaved ICY metadata chunks to worry about.
        buffered = bytearray()
        try:
            with urllib.request.urlopen(STREAM_URL, timeout=10) as resp:
                while not stop_event.is_set():
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buffered.extend(chunk)
                    with self._lock:
                        target_bytes = int(self._delay_seconds * AUDIO_BYTES_PER_SECOND)
                    release_len = max(0, len(buffered) - target_bytes)
                    if not release_len:
                        continue
                    out = bytes(buffered[:release_len])
                    del buffered[:release_len]
                    try:
                        # Blocks once the subprocess's stdin pipe (and its
                        # own internal queues) are full, which naturally
                        # paces releasing a backlog to real consumption
                        # speed rather than dumping it all instantly.
                        proc.stdin.write(out)
                        proc.stdin.flush()
                    except (BrokenPipeError, OSError):
                        return
        except OSError:
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
            elif value and value.startswith(NUDGE_MARKER_PREFIX):
                try:
                    seconds_delta = json.loads(value[len(NUDGE_MARKER_PREFIX):])["seconds"]
                except (ValueError, KeyError, TypeError):
                    continue
                new_delay = radio.nudge(seconds_delta)
                _call(sock, 7, "Runtime.evaluate", {"expression": _set_offset_js(new_delay)})
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
