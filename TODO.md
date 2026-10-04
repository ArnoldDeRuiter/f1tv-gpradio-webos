# TODO

## Resolved (2026-10-04)

What looked like one unfixable mystery stutter turned out to be three
separate, now-fixed bugs:

1. **Stutter-then-silence after ~60s.** Root cause: the feeder paced writes
   against a fixed `AUDIO_BYTES_PER_SECOND` guess, but the stream is VBR
   (confirmed via `alsasink`'s own bitrate tag messages). Drift against
   gst's small rewind-friendly queue reliably broke playback. Fixed by
   dropping rate-guessing entirely: shrink the OS pipe
   (`fcntl.F_SETPIPE_SZ`) so a plain blocking `write()` naturally paces to
   gst's real consumption rate, the same mechanism `souphttpsrc` uses
   internally.
2. **Duplicate keydown listeners** accumulated across many daemon restarts
   on the same long-lived page (up to 10 copies observed for one real
   keypress). Root webOS/CDP mechanism never fully identified; worked
   around by always removing-then-reattaching named listeners on every
   script execution, plus a `window`-shared debounce (`fireOnce`) that
   collapses same-marker duplicates firing within 50ms into one.
3. **Real audible stutter + ~10s rewind latency**, caused by two things
   stacked together: `fdsrc do-timestamp=true` stamped buffers by
   wall-clock arrival time rather than actual audio duration, so bursty
   writes made gst's `queue` think it was nearly empty when it was
   actually holding seconds of content — its `max-size-time=300ms` cap
   never meaningfully triggered. Fixed by moving `queue` to sit after
   `aacparse` instead (real timestamps from decoded AAC frame headers,
   immune to write-timing jitter). Separately, this TV runs with
   `load1≈17` nearly constantly (F1TV's own video decode) and a
   scheduling delay at the ALSA/hardware layer could clip audio for a
   moment without tripping any error in gst's own bookkeeping — fixed by
   giving `gst-launch-1.0` `SCHED_FIFO` realtime priority right after
   spawning it, so it isn't starved under load.

Confirmed live: smooth playback for several minutes at a time, rewind
latency dropped from ~10s to sub-second, `max_write_gap` dropped from a
routine 8-9s to ~0.3s.

## Open

- F1TV's own video stream occasionally crashes the app on start (same
  pre-existing issue as `f1tv-webos`, not something this app's code can
  fix) — just reopen, works on retry.
- **Rewind/forward latency could be tightened further (currently ~sub-
  second, "pretty fast but not instant").** Deliberately left as-is for
  now — current latency is fine for syncing radio commentary to video
  (not a task needing frame-accuracy), and every knob below trades
  directly against the CPU-contention robustness just fixed. Revisit only
  if a tighter sync feel is specifically wanted, and re-test the stutter
  fix carefully afterward.

  Where the current latency actually comes from (each stage adds delay
  between a rewound byte being written and it reaching the speaker):
  - OS pipe behind `proc.stdin`, shrunk via `fcntl.F_SETPIPE_SZ` to
    `SHRUNK_PIPE_SIZE_BYTES` (8192 bytes ≈ 0.5s of this stream's
    compressed bitrate).
  - gst's own `queue` element, `max-size-time=300000000` (0.3s).
  - `avdec_aac` decode + `audioconvert`: negligible (tens of ms).
  - PulseAudio's own client buffering on the ALSA-plugin sink-input
    (`pactl list sink-inputs` showed `Buffer Latency` ~190-200ms,
    `Sink Latency` ~15-20ms) -- the single biggest remaining chunk, and
    the least explored (not an explicit knob in `GST_FEED_CMD` today,
    would need `alsasink`'s own `buffer-time`/`latency-time` properties).

  **What could be tried, and the tradeoff each one makes:**
  - Shrink `SHRUNK_PIPE_SIZE_BYTES` further (toward the ~4096-byte page-size
    floor `F_SETPIPE_SZ` rounds up to). *Benefit*: ~0.25s off. *Risk*:
    smaller pipe means `write()` blocks (and the write thread wakes up)
    more often -- more syscall overhead, and less slack to absorb a brief
    scheduling delay before a write stalls audibly.
  - Lower gst `queue`'s `max-size-time` (e.g. 300ms -> 100-150ms).
    *Benefit*: proportional latency drop. *Risk*: this is the exact knob
    that interacts with this TV's CPU contention (`load1≈17` from F1TV's
    video decode) -- too tight and a scheduling delay empties the queue
    before new data arrives, reintroducing the stutter this session just
    fixed.
  - Tune PulseAudio's client-side buffer (`buffer-time`/`latency-time` on
    `alsasink`, or `PULSE_LATENCY_MSEC`). *Benefit*: biggest single win
    available (~190-200ms). *Risk*: least tested of the three -- unclear
    how low this TV's own `lg115x` ALSA sink/PulseAudio bridge can go
    before underrunning for reasons unrelated to our own feed logic
    entirely (real hardware/driver limit, not something `gpradio_overlay.py`
    controls).

  Combined, a realistic estimate is rewind latency dropping from ~1s to
  roughly 400-500ms -- worthwhile only if that's actually noticeable/
  wanted, and only with careful re-testing of the stutter fix (several
  minutes of clean playback, watching `max_write_gap` in
  `/tmp/f1tvgpradio-overlay.log`'s "feed debug" lines) after each change,
  one knob at a time.
