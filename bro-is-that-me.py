#!/usr/bin/env python3
"""
bro-is-that-me — generate a candid "you, right now" photo, styled like a still
frame pulled from a nearby security camera.

Self-contained, cross-platform (Windows / macOS / Linux). It:
  1. asks which LLM backend runs the agent, and which image generator to use
  2. verifies auth (API keys read silently, never stored, never in argv)
  3. checks the environment and requires an explicit typed consent
  4. spawns a HARD-SANDBOXED agent (writes confined to one temp folder by the OS)
     that figures out your platform, locates you, finds reference photos of the
     place, identifies you from your own photos, reads your device, and renders
     the image
  5. shows a live progress bar (no agent chain-of-thought, no extra windows)
  6. moves the result next to your other pictures and opens it

Run:  python3 bro-is-that-me.py     (Windows: python bro-is-that-me.py)
"""

import os
import re
import sys
import time
import json
import shutil
import signal
import getpass
import platform
import tempfile
import threading
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Config — model ids and timeouts. Adjust if a model id changes.
# ---------------------------------------------------------------------------
CLAUDE_MODEL = "claude-sonnet-5"     # Claude backends
CODEX_MODEL  = "gpt-5.6-terra"       # OpenAI / codex backends
EFFORT       = "medium"              # reasoning effort (codex)

OVERALL_TIMEOUT = 1500               # hard cap (s) — kill a run that never ends
IDLE_TIMEOUT    = 480                # kill if the agent emits nothing for this long

STEPS = [
    "Detecting platform & tools",
    "Locating you",
    "Resolving the address",
    "Finding photos of the place",
    "Identifying you",
    "Reading your device",
    "Generating the image",
    "Finalizing",
]
N_STEPS = len(STEPS)

# ===========================================================================
# Terminal helpers
# ===========================================================================
if os.name == "nt":
    os.system("")  # enable ANSI escape processing on Windows 10+
_TTY = sys.stdout.isatty()
def _c(code): return code if _TTY else ""
B   = _c("\033[1m");  DIM = _c("\033[2m"); RED = _c("\033[1;31m")
YEL = _c("\033[1;33m"); GRN = _c("\033[1;32m"); CYN = _c("\033[1;36m")
INV = _c("\033[7m");  R = _c("\033[0m")

def say(msg=""): print(msg)
def ok(msg):     print(f"{GRN}✓{R} {msg}")
def warn(msg):   print(f"{YEL}!{R} {msg}")
def die(msg):    print(f"{RED}✗ {msg}{R}", file=sys.stderr); sys.exit(1)
def hr():        print(f"{DIM}" + "─" * 60 + f"{R}")
def ask(prompt): return input(prompt).strip()

# ===========================================================================
# HTTP (stdlib) — key validation without extra deps
# ===========================================================================
def http_status(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None

# ===========================================================================
# Embedded agent instructions (platform-agnostic). @@TOKENS@@ get replaced.
# ===========================================================================
AGENT_TEMPLATE = r"""
# TASK: produce a "security-camera" style candid photo of this computer's owner,
# where they physically are right now.

You are an autonomous agent running non-interactively. Work end to end without
asking questions. All shell commands are pre-approved.

## Environment & hard rules
- You are running on an unknown OS. FIRST detect it (Windows, macOS, or Linux)
  and adapt every command accordingly. The commands shown below are EXAMPLES
  from one platform — translate them to the actual OS, shell, and toolchain you
  find (PowerShell on Windows, POSIX shell on macOS/Linux, or just use Node.js,
  which is cross-platform, for HTTP and OS info).
- Your writes are confined by the OS to this working directory:
    @@WORKDIR@@
  Do ALL work there. Trying to write elsewhere will be DENIED by the sandbox.
- The final image MUST end up at exactly this path: @@FINAL@@
- ATTACH real photo files to the image model. NEVER describe a person or place
  in words when you have an actual photo file to attach.
- PROGRESS PROTOCOL: after finishing each of the 8 numbered steps below, print a
  single line of the exact form `@@PROGRESS k@@` (k = the step number). Print
  nothing sensitive. These markers drive a progress bar; keep other output terse.
- When completely done, print exactly one final line: `@@RESULT @@FINAL@@`

---

### Step 1 — Detect platform & ensure tooling
Identify OS/arch. Ensure Node.js is available (it is cross-platform; use it for
HTTP and OS facts). Install Playwright locally in the working dir for headless
geolocation:
    npm init -y && npm i playwright
Use the already-installed system Chrome/Chromium (Playwright channel "chrome",
or "msedge"/"chromium" as a fallback) — do not download a browser if one exists.
Then: `@@PROGRESS 1@@`

### Step 2 — Get precise location via headless browser geolocation
Write `geo.mjs` and run `node geo.mjs`:
```javascript
import { chromium } from "playwright";
const origin = "https://example.com";
const browser = await chromium.launch({ channel: "chrome", headless: true });
try {
  const context = await browser.newContext();
  await context.grantPermissions(["geolocation"], { origin });
  const page = await context.newPage();
  await page.goto(origin);
  const loc = await page.evaluate(() => new Promise((res) => {
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => res({ latitude: coords.latitude, longitude: coords.longitude, accuracyMeters: coords.accuracy }),
      ({ code, message }) => res({ error: message, code }),
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
  }));
  console.log(JSON.stringify(loc));
} finally { await browser.close(); }
```
If it returns an `error` (location services off / headless blocked), fall back to
coarse IP geolocation (e.g. fetch `https://ipinfo.io/json`) and note lower
accuracy. Then: `@@PROGRESS 2@@`

### Step 3 — Reverse geocode coordinates -> address
Send the lat/long to OpenStreetMap Nominatim with a descriptive User-Agent:
    https://nominatim.openstreetmap.org/reverse?lat=LAT&lon=LON&format=json&addressdetails=1
(Use Node `fetch`, `curl`, or PowerShell `Invoke-RestMethod` — whatever exists.)
Determine the venue and what KIND of place it is (cafe, park, office, store,
home, street, transit, etc.). Then: `@@PROGRESS 3@@`

### Step 4 — Find reference photos of the place
Search the web for real photos of THIS location; save 2-4 good ones into
`refs/` inside the working dir (verify each is a real image).
- Public venue (cafe, shop, park, landmark): find EXACT photos of that specific
  place (business name + city; interior/seating/exterior).
- Private residence: you likely can't find the interior — grab 1-2 photos of a
  representative room of that style and make a tasteful, generic guess.
Then: `@@PROGRESS 4@@`

### Step 5 — Identify the owner among their own photos
Find a clear, recent, front-facing photo of this computer's owner. Look in
whatever personal image folders exist for this OS/user (the standard pictures
directory, camera-roll / DCIM, screenshots, webcam captures, downloads). Use
context clues: the face that recurs most across self-portraits, the most recent
clear frontal face. Convert to PNG if needed. Copy the single best one to
`user.png` in the working dir. If you truly find no person, print
`@@RESULT ERROR no user photo found` and stop. Then: `@@PROGRESS 5@@`

### Step 6 — Read the device (for a plausible on-screen look)
Determine device type, OS, and what is plausibly on screen right now (this tool
runs in a terminal, so a terminal / coding session is likely). Use OS-appropriate
introspection (e.g. Node `os` module cross-platform; `systeminfo` on Windows,
`system_profiler`/`sysctl` on macOS, `lscpu`/`/etc/os-release` on Linux; list of
foreground GUI apps if easy). Then: `@@PROGRESS 6@@`

### Step 7 — Compose the synthesis prompt (SECURITY-CAMERA STYLE)
Write a detailed prompt for ONE image that looks like a still frame captured by a
fixed **security / surveillance camera** that happens to see this person at this
place. Requirements:
- Preserve the REAL person from `user.png` EXACTLY: face, hair, eyebrows, skin
  tone, build, identity. Keep clothing plausible/casual.
- Place them naturally at the REAL location (from the reference photos), matched
  to the current local **time of day and weather**.
- Their device is open in front of them; the screen is faintly visible showing
  what they're doing (a terminal / coding session, or the app you detected).
- CAMERA LOOK: elevated, wide-angle vantage as if the camera is mounted high in a
  corner or on a wall; mild wide-angle lens distortion; slightly cool/flat
  surveillance color and exposure; subtle sensor noise; a small unobtrusive
  timestamp/timecode overlay in a corner is fine. IMPORTANT: keep it a REALISTIC,
  legible photograph with believable angles — NOT extreme low quality, not heavy
  pixelation, not a cartoon. Think a good modern HD CCTV frame.
Then: `@@PROGRESS 7@@`

### Step 8 — Generate, verify, clean up
@@IMGGEN_BLOCK@@

Verify the output at `@@FINAL@@` is a valid PNG; if the generator wrote it
elsewhere, move it to `@@FINAL@@`. Delete all scratch you created (node_modules,
package files, geo.mjs, refs/, user.png, intermediates) — leave ONLY the final
image. Then print:
    @@PROGRESS 8@@
    @@RESULT @@FINAL@@
"""

IMGGEN_CODEX = r"""Generate with codex, ATTACHING the real photos (do not describe them). The first
image is the person; the rest are the location:

    codex exec -C "@@WORKDIR@@" -s workspace-write --skip-git-repo-check \
      --image user.png refs/loc1.png refs/loc2.png \
      'Use the image generation tool to produce ONE photorealistic still frame
       that looks like SECURITY-CAMERA / CCTV footage, by EDITING/COMPOSING the
       attached images. The FIRST attached image is the REAL PERSON — preserve
       their face, hair, eyebrows, skin tone and identity EXACTLY. The remaining
       images are the REAL LOCATION — match it faithfully. <PLUG IN YOUR STEP-7
       SECURITY-CAMERA SYNTHESIS PROMPT>. Save as @@FINAL@@'

`--image` accepts multiple files. If your codex build rejects multiple, attach
only user.png and describe the location precisely from the reference photos."""

IMGGEN_OPENAI = r"""Generate with the OpenAI images edit API (gpt-image-1). The key is already in
the OPENAI_API_KEY environment variable. ATTACH the real photos as files (do not
describe them); the first is the person, the rest are the location:

    curl -s https://api.openai.com/v1/images/edits \
      -H "Authorization: Bearer $OPENAI_API_KEY" \
      -F model="gpt-image-1" -F input_fidelity="high" -F size="1536x1024" \
      -F "image[]=@user.png" -F "image[]=@refs/loc1.png" -F "image[]=@refs/loc2.png" \
      -F prompt="Produce ONE photorealistic still frame that looks like
        SECURITY-CAMERA / CCTV footage, from the attached images. The FIRST image
        is the REAL PERSON — preserve face, hair, eyebrows, skin tone and identity
        EXACTLY. The rest are the REAL LOCATION — match it faithfully. <PLUG IN
        YOUR STEP-7 SECURITY-CAMERA SYNTHESIS PROMPT>." \
      -o resp.json

Then decode the base64 result to @@FINAL@@ (cross-platform, e.g. with Node or
Python):
    python3 -c "import json,base64,sys; d=json.load(open('resp.json')); open(r'@@FINAL@@','wb').write(base64.b64decode(d['data'][0]['b64_json']))"
(On Windows use `python` instead of `python3`.)"""

# ===========================================================================
# Sandbox construction — the whole point: the agent CANNOT write outside WORKDIR.
# ===========================================================================
def seatbelt_profile(workdir, tmpdir, home):
    """macOS: allow everything, then deny ALL writes, then re-allow a tiny
    allowlist. The user's Documents/Pictures/Desktop stay write-denied.
    Only /dev/null is exposed from /dev (not the whole device tree)."""
    writable_subpaths = [
        "/private/tmp", "/private/var/folders", "/var/tmp",
        str(tmpdir), str(workdir),
        str(home / ".claude"), str(home / ".codex"), str(home / ".config"),
        str(home / ".cache"), str(home / ".npm"),
        str(home / "Library" / "Caches"),
    ]
    writable_literals = ["/dev/null"]
    rules = "".join(f'  (subpath "{p}")\n' for p in writable_subpaths)
    rules += "".join(f'  (literal "{p}")\n' for p in writable_literals)
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*)\n"
        f"(allow file-write*\n{rules})\n"
    )

def wrap_agent_command(base_cmd, runner, workdir, tmpdir, home):
    """Return (cmd, note). Raises if a hard sandbox can't be established for a
    runner that needs one (Claude), so we never silently fall back to prompt
    trust. `override` env lets an operator bypass at their own risk."""
    system = platform.system()
    override = os.environ.get("BRO_ALLOW_UNSANDBOXED") == "1"

    if runner == "codex":
        # codex sandboxes itself via -s workspace-write (already in base_cmd):
        # Seatbelt on macOS, Landlock+seccomp on Linux — kernel-enforced.
        if system in ("Darwin", "Linux"):
            mech = "Seatbelt" if system == "Darwin" else "Landlock/seccomp"
            return base_cmd, f"codex native OS sandbox (workspace-write, {mech})"
        # Native Windows (WSL reports as Linux): no reliable kernel sandbox.
        if override:
            return base_cmd, "UNSANDBOXED (override) — native Windows has no kernel write-sandbox"
        die("On native Windows, codex has no kernel-level write sandbox. Run under "
            "WSL (recommended — gives real confinement) or set BRO_ALLOW_UNSANDBOXED=1 "
            "to proceed at your own risk.")

    # runner == "claude": Claude Code has NO native OS sandbox, so we impose one.
    if system == "Darwin":
        prof = seatbelt_profile(workdir, tmpdir, home)
        return ["sandbox-exec", "-p", prof, *base_cmd], "macOS Seatbelt (sandbox-exec), default-deny writes"
    if system == "Linux":
        if shutil.which("bwrap"):
            binds = ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                     "--bind", str(workdir), str(workdir), "--bind", str(tmpdir), str(tmpdir)]
            for d in [home / ".claude", home / ".config", home / ".cache", home / ".npm"]:
                if d.exists():
                    binds += ["--bind", str(d), str(d)]
            return ["bwrap", *binds, "--share-net", "--die-with-parent", "--", *base_cmd], \
                   "Linux bubblewrap (bwrap), read-only root + writable workdir"
        if os.environ.get("BRO_ALLOW_UNSANDBOXED") == "1":
            return base_cmd, "UNSANDBOXED (override) — install bubblewrap for hard confinement"
        die("Claude runner needs bubblewrap (bwrap) for a hard sandbox on Linux. "
            "Install it (e.g. `apt install bubblewrap`), pick the codex runner, "
            "or set BRO_ALLOW_UNSANDBOXED=1 to proceed at your own risk.")
    # Windows / other with Claude
    if os.environ.get("BRO_ALLOW_UNSANDBOXED") == "1":
        return base_cmd, "UNSANDBOXED (override)"
    die(f"No hard sandbox available for the Claude runner on {system}. Use the "
        "codex runner (self-sandboxing), run under WSL, or set "
        "BRO_ALLOW_UNSANDBOXED=1 to proceed at your own risk.")

# ===========================================================================
# Progress bar driven by @@PROGRESS k@@ markers on the agent's stdout
# ===========================================================================
class Progress:
    def __init__(self):
        self.step = 0
        self.start = time.time()
        self.lock = threading.Lock()
        self.done = False

    def render(self):
        if not _TTY:
            return
        width = 28
        filled = int(width * self.step / N_STEPS)
        bar = "█" * filled + "─" * (width - filled)
        pct = int(100 * self.step / N_STEPS)
        label = STEPS[min(self.step, N_STEPS - 1)] if self.step < N_STEPS else "Done"
        el = int(time.time() - self.start)
        clock = f"{el // 60:02d}:{el % 60:02d}"
        sys.stdout.write(f"\r{CYN}{bar}{R} {pct:3d}%  {B}{label:<26}{R} {DIM}{clock}{R}")
        sys.stdout.flush()

    def set_step(self, k):
        with self.lock:
            self.step = max(self.step, min(k, N_STEPS))
            self.render()

def run_agent(cmd, env, instructions_path, progress):
    """Spawn the agent, stream stdout, drive the progress bar, enforce timeouts.
    Returns (result_path_or_None, error_or_None)."""
    popen_kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=open(instructions_path, "r"), text=True, bufsize=1, env=env)
    if os.name == "nt":
        popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kw["start_new_session"] = True
    p = subprocess.Popen(cmd, **popen_kw)

    state = {"last": time.time(), "result": None, "verbose": os.environ.get("BRO_VERBOSE") == "1"}
    prog_re = re.compile(r"@@PROGRESS\s+(\d+)@@")
    res_re = re.compile(r"@@RESULT\s+(.+?)\s*$")

    def reader():
        for line in p.stdout:
            state["last"] = time.time()
            m = prog_re.search(line)
            if m:
                progress.set_step(int(m.group(1)))
            r = res_re.search(line)
            if r:
                state["result"] = r.group(1).strip()
            if state["verbose"]:
                sys.stdout.write("\n" + DIM + line.rstrip() + R + "\n")
                progress.render()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    # ticking clock + watchdog
    while p.poll() is None:
        progress.render()
        now = time.time()
        if now - progress.start > OVERALL_TIMEOUT:
            kill_tree(p); return None, f"overall timeout ({OVERALL_TIMEOUT}s) — agent killed"
        if now - state["last"] > IDLE_TIMEOUT:
            kill_tree(p); return None, f"no output for {IDLE_TIMEOUT}s — agent looked stuck, killed"
        time.sleep(1)
    t.join(timeout=5)
    rc = p.returncode
    if state["result"] and state["result"].startswith("ERROR"):
        return None, state["result"]
    if rc != 0 and state["result"] is None:
        return None, f"agent exited with code {rc}"
    return state["result"], None

def kill_tree(p):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            time.sleep(2)
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:
        try: p.kill()
        except Exception: pass

# ===========================================================================
# Output location + open (cross-platform)
# ===========================================================================
def pictures_dir():
    home = Path.home()
    for name in ["Pictures", "Desktop", "Documents"]:
        d = home / name
        if d.is_dir() and os.access(d, os.W_OK):
            return d
    return home

def open_file(path):
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        elif os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)])
    except Exception:
        pass

# ===========================================================================
# Main
# ===========================================================================
def main():
    say(f"{B}bro-is-that-me{R}  {DIM}· a candid photo of you, where you are, right now{R}")
    say("")

    # 1) LLM backend
    say(f"{B}1) LLM backend to run the agent{R}")
    say(f"   {DIM}1) codex             (OpenAI Codex CLI, existing login){R}")
    say(f"   {DIM}2) claude code       (Claude Code CLI, existing login){R}")
    say(f"   {DIM}3) openai api key    (Codex CLI, key you paste now){R}")
    say(f"   {DIM}4) anthropic api key (Claude Code CLI, key you paste now){R}")
    choice = ask("   choose [1-4]: ")
    runner, auth = {"1": ("codex", "login"), "2": ("claude", "login"),
                    "3": ("codex", "openai_key"), "4": ("claude", "anthropic_key")}.get(choice, (None, None))
    if runner is None:
        die("invalid choice")
    ok(f"backend: {B}{runner}{R} ({auth})")
    say("")

    # 2) image generator
    say(f"{B}2) Image generator{R}")
    say(f"   {DIM}1) codex       (codex built-in image tool){R}")
    say(f"   {DIM}2) openai api  (gpt-image-1 images/edits){R}")
    say(f"   {DIM}   (Anthropic has no image model, so 'openai api' is the only non-codex option){R}")
    imgchoice = ask("   choose [1-2]: ")
    imggen = {"1": "codex", "2": "openai_api"}.get(imgchoice)
    if imggen is None:
        die("invalid choice")
    ok(f"image generator: {B}{imggen}{R}")
    say("")

    # 3) auth — keys read silently, kept only in this process, passed via env
    say(f"{B}3) Authentication{R}")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if runner == "codex":
        if not shutil.which("codex"):
            die("codex CLI not found — install it and run 'codex login'")
        if auth == "openai_key":
            if not openai_key:
                openai_key = getpass.getpass("   paste OpenAI API key (hidden): ")
            if http_status("https://api.openai.com/v1/models",
                           {"Authorization": f"Bearer {openai_key}"}) != 200:
                die("OpenAI key rejected")
            ok("OpenAI API key valid")
        else:
            ok("codex CLI present")
    else:
        if not shutil.which("claude"):
            die("claude CLI not found — install Claude Code")
        if auth == "anthropic_key":
            if not anthropic_key:
                anthropic_key = getpass.getpass("   paste Anthropic API key (hidden): ")
            if http_status("https://api.anthropic.com/v1/models",
                           {"x-api-key": anthropic_key, "anthropic-version": "2023-06-01"}) != 200:
                die("Anthropic key rejected")
            ok("Anthropic API key valid")
        else:
            ok("claude CLI present (existing login)")

    if imggen == "codex":
        if not shutil.which("codex"):
            die("codex CLI needed for image generation — install it and run 'codex login'")
        ok("codex available for image generation")
    else:
        if not openai_key:
            openai_key = getpass.getpass("   paste OpenAI API key for image gen (hidden): ")
        if http_status("https://api.openai.com/v1/models",
                       {"Authorization": f"Bearer {openai_key}"}) != 200:
            die("OpenAI key rejected")
        ok("OpenAI image API key valid")
    say("")

    # 4) environment
    say(f"{B}4) Environment{R}")
    chrome_spots = [
        "/Applications/Google Chrome.app",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    ]
    if any(Path(s).exists() for s in chrome_spots) or shutil.which("google-chrome") or shutil.which("chromium"):
        ok("a Chrome/Chromium browser was detected")
    else:
        if not ask("   No Chrome/Chromium found. Continue anyway (geolocation may fall back to IP)? [y/N]: ").lower().startswith("y"):
            die("A Chromium-based browser is needed for precise geolocation.")
    say(f"   {DIM}Precise geolocation needs OS Location Services enabled for the browser.{R}")
    if ask("   Are Location Services enabled for your browser? [Y/n]: ").lower().startswith("n"):
        warn("Will fall back to coarse IP-based (city-level) location.")
    say("")

    # 5) consent gate
    say(f"{RED}{INV} ⚠  READ BEFORE CONTINUING  ⚠ {R}")
    say(f"{B}This tool will, on your behalf:{R}")
    for line in [
        "Determine your PRECISE location via headless-browser geolocation (fallback: IP/city).",
        "Send your coordinates to OpenStreetMap (Nominatim) to get a street address.",
        "Run web searches to find public photos of where you are.",
        "Scan your personal photo folders to identify what you look like.",
        "Inspect your device model, OS, and (if easy) open apps.",
        f"Send your photo(s) + location reference images + prompt to the image provider ({imggen}).",
        f"Spawn an AI agent ({runner}) with commands auto-approved, HARD-sandboxed to one temp folder.",
    ]:
        say(f"  {RED}•{R} {line}")
    say(f"{B}Leaves your machine to:{R} OpenStreetMap (coords), web-search/image hosts, the image provider.")
    say("Nothing is written outside the temp folder except the final photo.")
    hr()
    if ask(f"{B}Type {GRN}I CONSENT{R}{B} to continue: {R}") != "I CONSENT":
        die("Aborted — no consent given.")
    ok("consent recorded")
    say("")

    # 6) sandbox workspace + rendered instructions
    home = Path.home()
    tmpdir = Path(tempfile.gettempdir())
    workdir = Path(tempfile.mkdtemp(prefix="bro-is-that-me.", dir=str(tmpdir)))
    (workdir / "refs").mkdir(exist_ok=True)
    final_png = workdir / "final.png"

    imggen_block = (IMGGEN_CODEX if imggen == "codex" else IMGGEN_OPENAI)
    instructions = (AGENT_TEMPLATE
                    .replace("@@IMGGEN_BLOCK@@", imggen_block)
                    .replace("@@WORKDIR@@", str(workdir))
                    .replace("@@FINAL@@", str(final_png)))
    instr_path = workdir / "INSTRUCTIONS.md"
    instr_path.write_text(instructions, encoding="utf-8")

    # 7) build agent command + env
    env = os.environ.copy()
    if openai_key:
        env["OPENAI_API_KEY"] = openai_key
    if anthropic_key:
        env["ANTHROPIC_API_KEY"] = anthropic_key

    if runner == "codex":
        base_cmd = ["codex", "exec", "-C", str(workdir), "-s", "workspace-write",
                    "--skip-git-repo-check", "-m", CODEX_MODEL,
                    "-c", f"model_reasoning_effort={EFFORT}",
                    "-c", "sandbox_workspace_write.network_access=true", "-"]
    else:
        base_cmd = ["claude", "-p", "--model", CLAUDE_MODEL,
                    "--dangerously-skip-permissions", "--add-dir", str(workdir)]

    cmd, sandbox_note = wrap_agent_command(base_cmd, runner, workdir, tmpdir, home)
    ok(f"sandbox: {sandbox_note}")
    say(f"{DIM}workspace: {workdir}{R}")
    say("")

    # 8) run
    say(f"{B}Working…{R} {DIM}(hidden agent; ~a few minutes){R}")
    progress = Progress()
    progress.render()
    result, err = run_agent(cmd, env, instr_path, progress)
    print()  # newline after the bar

    # 9) finalize
    if err:
        shutil.rmtree(workdir, ignore_errors=True)
        die(err)
    if not final_png.exists() or final_png.stat().st_size == 0:
        shutil.rmtree(workdir, ignore_errors=True)
        die(f"agent finished but no image at {final_png}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = pictures_dir() / f"bro-is-that-me-{stamp}.png"
    shutil.copy2(final_png, dest)
    shutil.rmtree(workdir, ignore_errors=True)
    ok(f"saved: {B}{dest}{R}")
    say(f"{GRN}{B}Done.{R} Opening it now…")
    open_file(dest)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\naborted.")
        sys.exit(130)
