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

## Controls (while F1TV is open)

- **Green** — toggle Grand Prix Radio play/pause.
- **Yellow** — show/hide the widget (OLED burn-in guard — don't leave a
  static overlay up for a multi-hour race).
- **Blue** — nudge the radio's sync later: tap for +0.5s, hold for +5s.
- **Red** — nudge it earlier: tap for -0.5s, hold for -5s (floors at 0,
  can't make a live stream play earlier than live itself).

The widget shows the current sync offset next to the now-playing title.

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
