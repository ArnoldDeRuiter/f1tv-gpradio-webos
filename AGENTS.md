# AGENTS.md

Read `~/git/personal/slopbro/TV-HANDOVER.md` first for the TV's IP/SSH
access, CDP, root exec bridge, and standing rules. This file covers what's
specific to this repo.

## What this is

`type: web` Homebrew app (`nl.arnolderuiter.f1tvgpradio`) — F1TV full-screen
(same wrapper as f1tv-webos) plus a background daemon
(`gpradio_overlay.py`) that plays Grand Prix Radio's live audio at the same
time and shows a toggle-able widget over F1TV's page.

## Key constraint: F1TV's CSP blocks loading GPRadio directly

Confirmed live: F1TV's `media-src`/`connect-src` CSP blocks both an
injected `<audio src>` and a page-side `fetch()` to anything outside its
own allowlist (formula1.com/bitmovin/akamaized/etc). `streamtheworld.com`
and `grandprixradio.nl` are not on it. This is why real playback and the
now-playing fetch both happen in `gpradio_overlay.py` itself (a plain root
process, no CSP), not in the injected page JS — the page only shows state
and relays remote key presses back to the daemon via a `console.log`
marker. Don't try to move audio/fetch logic back into the page; it'll just
get silently blocked again.

## Key constraint: use `alsasink device=media`, not the platform's own sink

`audiosink`/`lgaudiosink` (LG's own GStreamer audio element) needs a
resource "index" negotiated through LG's proprietary media resource
manager — confirmed via dmesg's `lgao_feed: index is invalid`, and there is
**no** `umediaserver` binary/config anywhere on this TV's filesystem at all
(unlike the documented webOS OSE reference platform), so there's no public
API to do that negotiation. `alsasink device=media` instead routes through
this TV's actual underlying audio stack, PulseAudio — found by reading the
installed `plx-native` app's own `/etc/asound.conf` (present identically
outside any jail too). Confirmed audible live. Don't switch back to
`audiosink` without solving that resource-acquisition problem first.

## Key constraint: no PyGObject/Gst bindings on this TV

`python3 -c "import gi"` fails (`ModuleNotFoundError`) — there's no way to
control a persistent GStreamer pipeline's properties at runtime from
Python the normal way. The sync-offset feature instead works by having
`gpradio_overlay.py` fetch the live stream itself and feed raw bytes into
`gst-launch-1.0 ... fdsrc fd=0 ! queue max-size-time=300000000 ...` via the
subprocess's stdin, controlling how many bytes are held back before being
written. The explicit small `queue` cap matters: without it, decodebin/
alsasink maintain their own multi-second buffer and silently absorb any
upstream pacing changes with zero audible effect (confirmed live — the
widget's offset number updated fine, but nothing changed audibly until the
queue was capped).

## Build

```sh
sh build.sh
```
Hand-rolled `.ipk`, same pattern as f1tv-webos/family7-webos.

## Testing changes live

```sh
scp index.html loginfill.py kill-netflix.sh start-loginfill.sh \
  gpradio_overlay.py start-gpradio-overlay.sh \
  tvtje:/media/developer/apps/usr/palm/applications/nl.arnolderuiter.f1tvgpradio/
```
Then close and reopen the app on the TV — needed for both the daemon
restart (exec-bridge chain only runs at launch) and a fresh page load (the
injected JS has a `window.__gpradioOverlayInstalled` idempotency guard that
silently no-ops on an already-loaded document).

## Gotchas hit debugging this

- `pgrep -f`/`pkill -f <pattern>` can match their own invocation if run
  inline in an SSH command whose own command-line text contains that same
  pattern string — write the check to a script file instead when this
  matters (bit us more than once working on this app specifically).
- `luna-send` prints nothing at all over a plain non-interactive SSH exec
  unless a real pty is allocated (`ssh -tt ... < /dev/null`) — silently
  "succeeds" (exit 0) with zero output otherwise.

## Rules

- Never `git push` — Captain pushes himself.
