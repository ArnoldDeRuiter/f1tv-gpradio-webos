#!/usr/bin/env python3
"""Background daemon: autofills the F1TV login form, never submits it.

Same architecture as family7-webos/scrollfix.py: this app's index.html does
a one-time top-level navigation away to f1tv.formula1.com (and from there,
account.formula1.com for login), so any JS living in our own page dies on
navigation. Instead this runs as a background root process, polls the
on-device Chrome DevTools Protocol endpoint (127.0.0.1:9998) for this app's
WAM tab, and keeps a live debugger connection open so the fill re-applies
to every new document that tab loads (including the login page itself,
reached via a redirect chain, not the app's own first document).

Credentials come from a file deployed by the lgtv-playbook Ansible
playbook (ansible-vault encrypted at rest in git, decrypted only at deploy
time) -- this daemon runs as root already (same exec-bridge launch as
scrollfix.py) so it can read a root-only credentials file with no
additional privilege needed.

Only fills the two real login fields (input[name="Login"] and
input[name="Password"] -- confirmed live via CDP inspection of the actual
account.formula1.com React app, which renders several other same-page
forms -- forgot-password, reset-password, sign-up -- using different field
names). Never touches or clicks the sign-in button; that stays a manual,
deliberate action.
"""

import json
import os
import socket
import time
import urllib.request

CDP_HOST = "127.0.0.1"
CDP_PORT = 9998
APP_DESCRIPTION = "nl.arnolderuiter.f1tvgpradio"
POLL_INTERVAL_SECONDS = 3
CREDENTIALS_PATH = "/var/lib/webosbrew/tv-credentials.json"


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


def load_credentials():
    with open(CREDENTIALS_PATH) as f:
        data = json.load(f)
    creds = data["f1tv"]
    return creds["username"], creds["password"]


def build_fill_js(username, password):
    # JSON-encode the credentials as JS string literals -- handles quoting/
    # escaping correctly regardless of what characters the password has.
    username_js = json.dumps(username)
    password_js = json.dumps(password)
    return """
(function(){
  if (window.__loginFillInstalled) return;
  window.__loginFillInstalled = true;

  function setReactInputValue(el, value) {
    var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(el, value);
    el.dispatchEvent(new Event('input', {bubbles: true}));
  }

  var username = %s;
  var password = %s;
  var filled = false;

  function tryFill() {
    if (filled) return;
    var userField = document.querySelector('input[name="Login"]');
    var passField = document.querySelector('input[name="Password"]');
    if (!userField || !passField) return;
    if (userField.value || passField.value) return;
    setReactInputValue(userField, username);
    setReactInputValue(passField, password);
    filled = true;
    console.log('__loginFillComplete__');
  }

  // Polling rather than a MutationObserver: the login form sits behind a
  // loading spinner for a while, and the framework appears to swap in a
  // fresh set of input nodes once real content replaces the spinner --
  // an observer watching the original (pre-swap) subtree can end up
  // reacting to nodes that get discarded, filling a node that's no longer
  // the one actually on screen. Polling always re-queries the live DOM,
  // so it doesn't matter how many times the form gets rebuilt.
  var pollInterval = setInterval(function(){
    tryFill();
    if (filled) clearInterval(pollInterval);
  }, 500);
})();
""" % (username_js, password_js)


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


AUTH_COOKIE_DOMAIN_SUFFIX = "formula1.com"
# Confirmed live: "login"/"user-metadata" linger as stale cookies even
# after logging out, so their mere presence is not proof of a real active
# session -- "entitlement_token" is cleared on logout and only reappears
# after a genuine fresh sign-in, so that's the one actually worth checking.
AUTH_COOKIE_NAME = "entitlement_token"


def _call(sock, msg_id, method, params=None):
    """Request/response helper: sends one CDP command and blocks for the
    matching reply, discarding any events that arrive first."""
    _send_frame(sock, json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        opcode, payload = _recv_frame(sock)
        if opcode != 0x1:
            continue
        try:
            msg = json.loads(payload.decode("utf-8"))
        except ValueError:
            continue
        if msg.get("id") == msg_id:
            return msg


def _has_valid_session(sock):
    """True if a cookie proving an already-logged-in session exists. Checked
    before injecting anything -- no point watching for a login form that
    won't even show up because the site will just load straight into the
    logged-in view."""
    result = _call(sock, 100, "Network.getAllCookies")
    cookies = result.get("result", {}).get("cookies", [])
    for cookie in cookies:
        domain = cookie.get("domain", "")
        if domain.endswith(AUTH_COOKIE_DOMAIN_SUFFIX) and cookie.get("name") == AUTH_COOKIE_NAME:
            return True
    return False


FILL_COMPLETE_MARKER = "__loginFillComplete__"


def _is_fill_complete_message(payload):
    try:
        msg = json.loads(payload.decode("utf-8"))
    except ValueError:
        return False
    if msg.get("method") != "Runtime.consoleAPICalled":
        return False
    args = msg.get("params", {}).get("args", [])
    return bool(args) and args[0].get("value") == FILL_COMPLETE_MARKER


def _watch_target(target, fill_js):
    """Blocks until the fields are filled, or the target closes (app closed) --
    whichever comes first. Once autofill succeeds there's nothing further for
    this connection to do, so it closes itself immediately rather than
    holding a root process + CDP connection open for the rest of the app's
    lifetime just to watch a login page that's no longer showing."""
    target_id = target["id"]
    ws_url = target["webSocketDebuggerUrl"]
    path = ws_url.split(CDP_HOST + ":" + str(CDP_PORT), 1)[1]
    sock = socket.create_connection((CDP_HOST, CDP_PORT), timeout=10)
    try:
        _handshake(sock, CDP_HOST, CDP_PORT, path)
        _call(sock, 99, "Network.enable")
        if _has_valid_session(sock):
            print("loginfill: already logged in, nothing to fill", flush=True)
            return "already-logged-in"
        _send_frame(sock, json.dumps({"id": 1, "method": "Page.enable"}))
        _send_frame(sock, json.dumps({"id": 2, "method": "Runtime.enable"}))
        _send_frame(
            sock,
            json.dumps(
                {
                    "id": 3,
                    "method": "Page.addScriptToEvaluateOnNewDocument",
                    "params": {"source": fill_js},
                }
            ),
        )
        _send_frame(
            sock, json.dumps({"id": 4, "method": "Runtime.evaluate", "params": {"expression": fill_js}})
        )
        print("loginfill: injected into target %s" % target_id, flush=True)
        sock.settimeout(60)
        while True:
            try:
                opcode, payload = _recv_frame(sock)
            except socket.timeout:
                continue
            if opcode == 0x8:
                return "closed"
            if opcode == 0x1 and _is_fill_complete_message(payload):
                return "filled"
    except OSError as exc:
        print("loginfill: target %s connection ended: %s" % (target_id, exc), flush=True)
        return "error"
    finally:
        try:
            sock.close()
        except OSError:
            pass


def main():
    print("loginfill: watching for %s" % APP_DESCRIPTION, flush=True)
    try:
        username, password = load_credentials()
    except (OSError, KeyError, ValueError) as exc:
        print("loginfill: no usable credentials at %s (%s), exiting" % (CREDENTIALS_PATH, exc), flush=True)
        return
    fill_js = build_fill_js(username, password)
    target = _find_target()
    reason = _watch_target(target, fill_js)
    print("loginfill: exiting (%s)" % reason, flush=True)


if __name__ == "__main__":
    main()
