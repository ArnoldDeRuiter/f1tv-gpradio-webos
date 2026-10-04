# F1TV + GP Radio (webOS)

Unofficial combined F1TV + Grand Prix Radio app for rooted LG webOS TVs.
Full-screen F1TV video, same as [f1tv-webos](https://github.com/ArnoldDeRuiter/f1tv-webos),
plus a small toggle-able widget that plays [Grand Prix Radio](https://grandprixradio.nl)'s
live audio commentary at the same time — something two separate webOS apps
can't do on their own (switching away from an app stops its audio/video, a
hard platform limit).

Not affiliated with Formula 1, F1TV, Liberty Media, or Grand Prix Radio.

## How it works

F1TV itself works exactly like `f1tv-webos` (full-screen redirect to
f1tv.formula1.com, webOS's own browser handles DRM/playback natively).

The radio part can't just be an `<audio>` element on F1TV's own page —
F1TV's CSP (`media-src`/`connect-src`) blocks loading anything from
`streamtheworld.com` or `grandprixradio.nl`, confirmed live. Instead, a
background root process (`gpradio_overlay.py`) fetches the stream and the
now-playing info itself (not subject to that CSP at all) and plays it
through `gst-launch-1.0 ! alsasink device=media` — this TV's real
underlying audio stack is PulseAudio (confirmed via the `plx-native` app's
own `/etc/asound.conf`), not the platform's own `lgaudiosink`/`audiosink`
GStreamer element, which needs a resource "index" negotiated through LG's
proprietary (and on this TV, undocumented/unreachable) media resource
manager.

The widget only shows state and relays remote button presses to that
background process via a `console.log` marker — the real playback and the
now-playing fetch both happen daemon-side.

Because the radio runs as a fully independent root process (not a child of
F1TV's own WAM renderer, and not going through webOS's own per-app audio
session at all), it keeps playing even if you leave F1TV entirely — go to
the home screen, open another app, whatever. A normal webOS app's audio
stops the moment it's backgrounded (that's enforced by the platform's own
per-app audio session lifecycle); this sidesteps that entirely just by not
being a "normal app" from the audio stack's point of view. Confirmed live:
going to the home screen while the radio was playing didn't stop it.

## Controls (while F1TV is open)

- **Green** — toggle Grand Prix Radio play/pause.
- **Yellow** — show/hide the widget (OLED burn-in guard — don't leave a
  static overlay up for a multi-hour race).
- **Red** — rewind the radio to sync it with the video by hand: tap for
  0.5s, hold for 5s. This is a real rewind (replays audio you already
  heard), not just a playback delay — a plain delay on a continuous live
  stream turned out to be audibly undetectable, confirmed live.
- **Blue** — seek forward (undo a rewind, back toward live): tap for 0.5s,
  hold for 5s. Clamped at the live edge (can't seek past "now") on one
  side and ~2 minutes of rewind room on the other.

The widget shows how far behind live the radio currently is, next to the
now-playing title.

## Known issues

- **The radio sometimes stutters and then goes completely silent after
  running for a while**, even with zero rewind applied and the whole
  signal chain (this daemon, gst-launch, PulseAudio's sink-input, the
  PulseAudio sink, the ALSA hardware mixer, the raw kernel PCM clock, and
  the actual decoded-audio level) all still reporting perfectly healthy at
  the exact moment it goes silent. Root cause not found despite extensive
  live debugging — looks like something below PulseAudio entirely (a
  hardware/amp-enable gate, or an LG-proprietary audio-routing policy) that
  isn't visible through any ALSA/PulseAudio/`/proc` interface. **Fix:
  toggle green off then on** — a full restart of the audio connection
  reliably recovers it, and your rewind position is preserved across that
  restart so you don't lose your sync.

## Login autofill

Same as `f1tv-webos` — reads `/var/lib/webosbrew/tv-credentials.json`'s
`f1tv` key if present. See that repo's README for the full setup; the file
format and behavior are identical here.

## Installing

In Homebrew Channel, open **Add repository** and enter the latest
release's `repo.json` URL (once a release exists), or build it yourself:

```sh
./build.sh
```
produces `nl.arnolderuiter.f1tvgpradio_<version>_all.ipk`, installable by
sideloading the ipk directly.

## License

MIT.
