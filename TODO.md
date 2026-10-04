# TODO

- **Radio stutters then goes fully silent after running a while, root cause
  unfound.** Confirmed live, repeatedly, over a long debugging session
  (2026-10-04): every layer of the signal chain reports perfectly healthy
  at the exact moment it goes silent —
  - `gpradio_overlay.py`'s own feeder thread: still reading/writing data in
    lockstep, no errors, no stalls (`/tmp/f1tvgpradio-overlay.log`).
  - `gst-launch-1.0` process: alive, no errors in its own captured stderr
    (`/tmp/f1tvgpradio-gst.log`), and a `level` element inserted into the
    pipeline shows real, loud, non-silent RMS/peak values on the actual
    decoded audio — i.e. the decoder is NOT producing silence.
  - PulseAudio's view of our stream (`pactl list sink-inputs`): not
    corked, not muted, 100% volume, connected to the real hardware sink.
  - PulseAudio's sink itself (`pactl list sinks`, sink `pcm_output`/
    `lg115x`): `State: RUNNING`, not muted, 100% volume.
  - The ALSA hardware mixer (`amixer`, `Master` control): 100%, on.
  - The raw kernel PCM device (`/proc/asound/card0/pcm6p+pcm7p/sub0/status`):
    `state: RUNNING`, `hw_ptr` genuinely advancing at ~44100Hz across
    repeated samples — real clock-out to hardware, confirmed twice.
  - `com.webos.audio/getVolume`: `muteStatus: false`, consistent
    `scenario: "mastervolume_tv_speaker"`.
  - Disabling PulseAudio's `module-suspend-on-idle` (a plausible suspect —
    it can suspend a sink based on its own idle heuristics even while a
    client looks attached) did NOT fix it.

  Found (but did not experiment with, too risky to poke blind): this TV
  has a large set of undocumented, proprietary ALSA mixer controls
  (`amixer -c0 controls`) including `Sndout Spk Output`, `Sndout
  MainAudio Output`, and a separate `Mute Output` register distinct from
  anything PulseAudio/ALSA's normal API surfaces — likely a hardware
  routing/amp-enable layer sitting below everything checked above. No
  `umediaserver` binary/config exists on this TV at all (see
  f1tv-webos's own notes on the same dead end for the `lgaudiosink`
  resource-manager path), so there's no public API to query or control
  this layer properly.

  **Confirmed, reliable recovery**: toggling green off then on (a full
  restart of the gst-launch subprocess + its PulseAudio connection) always
  brings it back. The rewind/seek position is preserved across that
  restart (see `RadioProcess.start()`'s `_preserved_lag_seconds` handling)
  so recovering doesn't cost you your sync work.

  Next steps for whoever picks this up: the undocumented `Sndout`/`Mute
  Output` mixer controls are the most promising unexplored lead, but
  changing them blind risks affecting F1TV's own audio too (they're
  TV-wide registers, not scoped to our stream) — would want to understand
  their semantics (read currently-correct values, change one at a time,
  confirm effect) before touching them for real. Also unexplored: whether
  PulseAudio's own debug log (started with `--log-level=debug`) is
  actually captured anywhere — `/var/log/messages` turned out to be
  webOS's own structured notification log (`NL_*` tagged), not generic
  syslog, despite pulseaudio's `--log-target=syslog` flag; never found
  where its debug output actually goes, if anywhere.
