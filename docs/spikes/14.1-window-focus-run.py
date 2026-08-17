"""Cross-instance window focus spike (tabscurator §14.1).

Protocol, per the design doc: exactly ONE chrome.windows.update({focused:true})
per attempt, a control arm with no call, >=60s dwell, and two window states.
Everything is measured from outside via System Events (frontmost process name +
unix id), so "the browser came forward" is not inferred from the browser itself.
"""
import http.server, json, subprocess, threading, time, os, signal, sys

PORT = 8777
BRAVE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
EXT = "/tmp/focus-spike/ext"
LOG = []
STATE = {"cmd": "wait"}
focus_served = False


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def do_OPTIONS(self):
        self.send_response(204); self._cors()
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        global focus_served
        cmd = STATE["cmd"]
        if cmd == "focus":
            if focus_served:          # server-side one-shot
                cmd = "wait"
            else:
                focus_served = True
        body = cmd.encode()
        self.send_response(200); self._cors()
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode()
        LOG.append((time.time(), json.loads(raw)))
        self.send_response(200); self._cors(); self.end_headers()


def frontmost():
    r = subprocess.run(["osascript", "-e",
        'tell application "System Events" to tell '
        '(first application process whose frontmost is true) '
        'to get {name, unix id}'],
        capture_output=True, text=True)
    s = r.stdout.strip()
    if r.returncode != 0 or not s:
        return ("QUERY-FAILED:" + r.stderr.strip()[:60], -1)
    parts = [p.strip() for p in s.rsplit(",", 1)]
    return (parts[0], int(parts[1])) if len(parts) == 2 else (s, -1)


def front_terminal():
    subprocess.run(["osascript", "-e",
        'tell application "Terminal" to activate'], capture_output=True)
    time.sleep(2)


def arm(name, treatment, minimize, fullscreen=False):
    global focus_served
    focus_served = False
    STATE["cmd"] = "wait"
    LOG.clear()
    prof = f"/tmp/focus-spike/prof-{name}"
    subprocess.run(["rm", "-rf", prof])
    p = subprocess.Popen([BRAVE, f"--user-data-dir={prof}",
                          f"--load-extension={EXT}", "--no-first-run",
                          "--no-default-browser-check", "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(12)
    STATE["cmd"] = "hello"; time.sleep(3)
    alive = any(e.get("event") == "alive" for _, e in LOG)

    if fullscreen:
        STATE["cmd"] = "fullscreen"; time.sleep(10)
        STATE["cmd"] = "wait"; time.sleep(2)
    if minimize:
        STATE["cmd"] = "minimize"; time.sleep(3)
    STATE["cmd"] = "wait"; time.sleep(1)

    front_terminal()
    base = frontmost()

    STATE["cmd"] = "focus" if treatment else "noop"
    samples = []
    t0 = time.time()
    for wait in (2, 5, 15, 30, 60):
        while time.time() - t0 < wait:
            time.sleep(0.3)
        samples.append((wait, frontmost()))
    STATE["cmd"] = "wait"

    called = [e for _, e in LOG
              if e.get("event") in ("focus_called", "fullscreen_setup", "minimized")]
    called.append({"noop_count": sum(1 for _, e in LOG if e.get("event") == "noop_arm")})
    res = {"arm": name, "treatment": treatment, "minimized": minimize,
           "fullscreen": fullscreen,
           "ext_alive": alive, "browser_pid": p.pid, "baseline": base,
           "samples": samples, "probe_events": called}
    p.send_signal(signal.SIGTERM)
    time.sleep(3)
    try: p.kill()
    except Exception: pass
    time.sleep(2)
    return res


srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

results = []
for nm, tr, mi, fs in [("fs-treat", True, False, True),
                       ("fs-control", False, False, True)]:
    results.append(arm(nm, tr, mi, fs))
    print(json.dumps(results[-1], ensure_ascii=False), flush=True)

with open("/tmp/focus-spike/results-fs2.json", "w") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("DONE", flush=True)
