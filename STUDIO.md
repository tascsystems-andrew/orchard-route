# Orchard Route on the Mac Studio

Deployment guide for the project's compute box: a **Mac Studio, M4 Max, 128 GB**
(16-core CPU / 40-core GPU bin). The dev laptop — MacBook Pro, M4 Pro, 48 GB,
20 GPU cores — stays the design machine; the Studio is where the big and long
runs happen, with its own remote Claude Code sessions so work can be handed
off rather than babysat.

Everything here was verified against the linked docs on **2026-07-18**. Claude
Code in particular moves fast — when this file and the docs disagree, the docs
win.

## Why the Studio matters

|                           | dev laptop (M4 Pro)     | Mac Studio (M4 Max)      |
|---------------------------|-------------------------|--------------------------|
| unified memory            | 48 GB                   | **128 GB**               |
| GPU cores                 | 20                      | **40**                   |
| memory bandwidth          | 273 GB/s                | **546 GB/s**             |
| lattice capacity estimate | ~9.6M nodes             | **~25.6M nodes**         |

Chip figures are Apple's published specs for the M4 Pro and the full
(40-core) M4 Max ([Apple newsroom](https://www.apple.com/newsroom/2024/10/apple-introduces-m4-pro-and-m4-max/)).
The capacity row uses OrthoRoute's own README sizing heuristic (~200k lattice
nodes per GB); the setup script prints the same estimate for whatever machine
it runs on. For scale: a 24 GB RTX 4090 is ~4.8M nodes by the same rule — the
unified-memory angle is the whole reason this project targets Apple Silicon.

What the extra headroom buys, concretely:

- **Fine pitch.** Halving pitch quadruples node count (see the pitch rule in
  `AGENTS.md`). Voxy-arduino at 0.5 mm is ~682k nodes; 0.25 mm is ~2.7M —
  comfortable on the laptop, but a 300×280 mm board at 0.25 mm with more
  layers heads toward the tens of millions, which is Studio territory.
- **Batch width.** `wavefront.py` was explicitly engineered for Studio scale:
  its init is built in MLX because a numpy `(B, N)` intermediate at 26M nodes ×
  512 planes would be a second ~53 GB allocation and OOM even 128 GB. Cap peak
  working set at ~80% of RAM and chunk the batch — the code already follows
  this; keep it true for new kernels.
- **Fleet throughput.** Calibration sweeps (via_cost, dir_penalty) across the
  whole bench fleet are embarrassingly parallel across runs — the Studio can
  grind those unattended while the laptop stays interactive.

Honest caveat: don't expect a blanket 2× wall-clock. Measured scaling on the
M4 Pro is *round-count/latency bound*, not compute bound (24k → 524k nodes
only moved a solve from 71 ms to 127 ms). The extra cores and bandwidth pay
off at high batch width and on huge lattices; the Studio's headline win is
**capacity and unattended throughput**, and the bench fleet will tell us the
real speedup. Measure before quoting numbers.

## One-time macOS prep (manual — the setup script never touches system settings)

On the Studio, signed in as your user:

1. **Remote Login (SSH):** Apple menu → System Settings → General → Sharing →
   turn on **Remote Login** (optionally "Allow full disk access for remote
   users"; the pane shows the exact `ssh user@host` command to use).
   [Apple's guide](https://support.apple.com/guide/mac-help/allow-a-remote-computer-to-access-your-mac-mchlp1066/mac).
2. **SSH keys:** from the laptop, `ssh-copy-id user@studio.local`, then add a
   `Host studio` block to `~/.ssh/config`. Turn off password auth later if
   the box is ever reachable from outside the LAN.
3. **Screen Sharing (optional but recommended):** System Settings → General →
   Sharing → **Screen Sharing**
   ([Apple's guide](https://support.apple.com/guide/mac-help/turn-screen-sharing-on-or-off-mh11848/mac)).
   Connect from the laptop via Finder → Go → Connect to Server →
   `vnc://studio.local`. You need this for anything that wants a GUI session:
   KiCad's IPC API, the Claude Desktop app on the Studio, and first-time
   login flows.
4. **Stay awake:** System Settings → Energy → enable "Prevent automatic
   sleeping when the display is off". Belt-and-suspenders for individual long
   runs: prefix them with `caffeinate -dims` (the bench instructions below do).
5. **FileVault caveat:** an encrypted boot disk waits at the pre-boot unlock
   screen after any power cycle — no SSH until someone types the password on
   a locally attached display. For a headless compute box either leave
   FileVault off (LAN-only box, your call) or keep a monitor/keyboard within
   reach for power events.
6. **Auto-login (only if/when KiCad IPC or the Desktop app should survive
   reboots unattended):** System Settings → Users & Groups → "Automatically
   log in as…". Incompatible with FileVault. Not needed for SSH/tmux or
   Remote Control work.

## Setup

```sh
mkdir -p ~/Code && cd ~/Code
git clone https://github.com/tascsystems-andrew/orchard-route.git
cd orchard-route
./scripts/studio-setup.sh
```

(Or, without cloning first:
`curl -fsSL https://raw.githubusercontent.com/tascsystems-andrew/orchard-route/main/scripts/studio-setup.sh | bash`
— the script clones for you, into `~/Code/orchard-route` by default,
`ORCHARD_DIR` to override.)

The script is idempotent — run it again any time; it only ever touches the
repo checkout and its `.venv`. It:

1. verifies Apple Silicon + macOS version, prints the machine's node-capacity
   estimate;
2. verifies Xcode CLT (git) and **Homebrew** are present — it prints the
   install command and stops rather than install system software silently;
3. clones the repo, or ff-only pulls an existing *clean* checkout (a dirty
   tree is never touched);
4. creates `.venv`, installs `mlx` + `numpy`;
5. runs the CPU-only smoke: a synthetic-board parse → lattice build (works on
   a bare machine), then `test_board.py` (its Voxy fixtures are laptop-local
   amp projects and skip gracefully elsewhere; the two bench-board fixtures
   join in once `bench/boards/` is fetched);
6. prints the **GPU validation steps for you to run yourself** —
   `spike_sssp.py` (kernel vs Dijkstra, expect 0 mismatches),
   `test_lattice.py`, and the bench baseline/route runs. The script itself
   never imports mlx or touches the GPU.

## Remote Claude Code on the Studio

Current options, verified against the Claude Code docs (July 2026):

| # | Option | Claude runs on | Laptop needed? | GUI session on Studio? | Docs |
|---|--------|----------------|----------------|------------------------|------|
| 1 | SSH + `claude` in tmux | Studio | Only to launch/attach | No | [setup](https://code.claude.com/docs/en/setup), [authentication](https://code.claude.com/docs/en/authentication) |
| 2 | **Remote Control** (`claude remote-control`) | Studio | **No** — drive from phone / any browser | No | [remote-control](https://code.claude.com/docs/en/remote-control) |
| 3 | Desktop app on laptop → **SSH session** env | Studio | Yes (Desktop is the UI) | No | [desktop → SSH sessions](https://code.claude.com/docs/en/desktop#ssh-sessions) |
| 4 | Desktop app **on the Studio** (+ Dispatch from phone) | Studio | No | **Yes** (logged-in GUI) | [desktop](https://code.claude.com/docs/en/desktop), [Dispatch](https://code.claude.com/docs/en/desktop#sessions-from-dispatch) |
| 5 | Claude Code on the web / `--cloud` / `--teleport` | **Anthropic cloud VM — not the Studio** | No | No | [claude-code-on-the-web](https://code.claude.com/docs/en/claude-code-on-the-web) |

### 1. Baseline: SSH + tmux (always works)

Install Claude Code on the Studio
([setup docs](https://code.claude.com/docs/en/setup)):

```sh
curl -fsSL https://claude.ai/install.sh | bash   # native installer, auto-updates
# or: brew install --cask claude-code            # note: does NOT auto-update
```

First login on a headless box: run `claude`, and if the browser-redirect
callback can't reach the Studio, the docs' fallback applies — the browser
shows a login code that you paste at the terminal's "Paste code here if
prompted" prompt; the docs call this out as the normal path for SSH sessions
([authentication](https://code.claude.com/docs/en/authentication)). So: copy
the login URL from the SSH terminal, open it in the *laptop's* browser, paste
the code back.

Then the classic pattern:

```sh
ssh studio
tmux new -s route        # or: tmux attach -t route
cd ~/Code/orchard-route && claude
# detach: Ctrl-B D — the session keeps running with the laptop closed
```

Fully independent of the laptop once launched. This is the fallback that
works regardless of feature rollouts.

Two macOS-specific practical notes (not from the Anthropic docs — from how
macOS works): credentials are stored in the **macOS Keychain**
([authentication](https://code.claude.com/docs/en/authentication)), and on a
box you only ever SSH into, the login keychain can be locked; if `claude`
can't read its credential, run
`security unlock-keychain ~/Library/Keychains/login.keychain-db` in the SSH
session, or log in once via Screen Sharing.

### 2. Remote Control — the intended handoff mechanism

This is the feature built for exactly this box. Start on the Studio (inside
tmux, so it survives disconnects):

```sh
tmux new -s rc
cd ~/Code/orchard-route
claude remote-control --spawn worktree --name "Studio"
```

Server mode waits for connections and serves **up to 32 concurrent sessions**
(`--capacity`), each in its own git worktree with `--spawn worktree` so
parallel sessions can't stomp each other's files. Open
[claude.ai/code](https://claude.ai/code) or the Claude iOS/Android app from
*any* device — laptop, phone, couch — and the Studio's sessions appear in the
list (default names are prefixed with the machine's hostname, so Studio
sessions are visually distinct from laptop ones). Alternatives: `claude
--remote-control` for a single interactive session you can also type into
locally, or `/remote-control` (`/rc`) from inside an already-running session.
All details: [Remote Control docs](https://code.claude.com/docs/en/remote-control).

Facts that matter for this deployment (all from that doc page):

- Execution and filesystem stay **on the Studio**; claude.ai/the phone app
  are a window into the local session. Outbound HTTPS only — no inbound
  ports, nothing to punch through the router.
- Requires claude.ai subscription login (Pro/Max). **API keys and
  `claude setup-token` tokens can't establish Remote Control** — full
  `/login` on the Studio is required.
- The `claude` process must keep running (hence tmux), and a machine that
  loses network for ~10 minutes has its session time out — restart with
  `claude remote-control -c` to resume.
- Mobile push notifications: enable in `/config` ("Push when Claude decides"
  / "Push when actions required") — the Studio can ping the phone when a long
  route finishes or a permission prompt is blocking.
- The session transcript is stored on Anthropic servers while connected (to
  sync devices); execution stays local.

### 3. Desktop app on the laptop, SSH environment

The Claude Desktop app's Code tab can run sessions on a remote machine over
SSH: environment dropdown → **+ Add SSH connection** → `andrew@studio.local`
(+ key). Claude then runs *on the Studio* with the Desktop app as the UI, and
Desktop auto-installs Claude Code on the remote host the first time it
connects ([desktop docs, SSH sessions](https://code.claude.com/docs/en/desktop#ssh-sessions)).
Nice for a GUI over Studio work, but the laptop must stay open — the Desktop
app is the interface. For fire-and-forget, use option 2.

### 4. Desktop app on the Studio + Dispatch

Running the Desktop app *on the Studio itself* gives Dispatch: message a task
from the phone's Cowork tab and it can spawn sessions on the Studio without
any laptop involvement ([Dispatch](https://code.claude.com/docs/en/desktop#sessions-from-dispatch),
[support article](https://support.claude.com/en/articles/13947068)), plus
Desktop [scheduled tasks](https://code.claude.com/docs/en/desktop-scheduled-tasks).
The cost: it's a GUI app, so the Studio needs a logged-in GUI session that
survives (auto-login, and Screen Sharing to manage it). Worth setting up in
the same pass as KiCad IPC, which has the same requirement — until then,
option 2 covers the handoff need.

### 5. What does NOT run on the Studio: Claude Code on the web

[Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web)
(`claude --cloud`, claude.ai/code "cloud" sessions) executes in
**Anthropic-managed cloud VMs**, not on your hardware — no Metal, no 128 GB,
no bench fleet. Fine for docs/refactor work on the repo from anywhere;
useless for routing. `--teleport` pulls a cloud session *down* to a terminal
(including the Studio's) to continue locally. Don't confuse `--cloud`
(cloud VM) with `--remote-control` (your machine) — the docs make the same
warning.

Also available on any machine, for completeness: non-interactive/scripted
runs (`claude -p`, [headless docs](https://code.claude.com/docs/en/headless)),
[Channels](https://code.claude.com/docs/en/channels) (drive a session from
Telegram/Discord), and [CLI scheduled tasks](https://code.claude.com/docs/en/scheduled-tasks).

## Running the bench fleet on the Studio

`bench/boards/` is **gitignored** (third-party boards, not redistributed) —
a fresh clone has only `bench/boards/SOURCES.md`. Re-fetch each board from
the origins pinned there (exact URLs, commits, zip sha256s), keeping the same
directory names — `run_bench.py` and `test_board.py` expect them:

- KiCad demos (`pic_programmer`, `video`): KiCad GitLab, pinned commit
- RPi `Minimal-KiCAD.zip` / `VGA-KiCAD.zip`: datasheets.raspberrypi.com,
  sha256 in SOURCES.md
- SparkFun IoT RedBoard RP2350: GitHub, pinned commit
- iCEBreaker v1.0e + Bitsy: Codeberg, pinned commit

Then:

```sh
cd ~/Code/orchard-route
.venv/bin/python bench/run_bench.py --mode baseline          # CPU-only: parses fleet, records human ground truth
caffeinate -dims .venv/bin/python bench/run_bench.py --mode route   # GPU: re-route fleet, ratios vs human
```

Results land in `bench/results.json` (deliberately timestamp-free so git
diffs runs). Compare against the scorecard in `README.md` — same fleet, M4
Pro numbers — for the first real laptop-vs-Studio datapoint.

Long-run hygiene, learned the hard way: launch long routes in the
**foreground of a tmux pane** — not as detached/backgrounded jobs, which die
or orphan when their parent exits. tmux *is* the backgrounding. For
calibration sweeps (via_cost / dir_penalty grids across the fleet), remember
the standing rule from the project notes: **never tune quality parameters on
Voxy-arduino** — its placement is deliberately rough; it's a
correctness/stress fixture. Calibrate on the human-placed bench fleet only.

## KiCad on the Studio

Needed for exactly two things — the router itself never requires KiCad:

1. **`kicad-cli` DRC as ground truth** on `writeback.py` output (today).
2. **The IPC API for Konnect** (future, when L0 lands).

Install: `brew install --cask kicad` (KiCad 10). The CLI binary lives inside
the app bundle:

```sh
/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli pcb drc \
    --format json --exit-code-violations out/routed.kicad_pcb
```

`--exit-code-violations` exits 5 when violations exist — scriptable pass/fail
([KiCad 10 CLI docs](https://docs.kicad.org/10.0/en/cli/cli.html)).

**Headless honesty note:** the KiCad docs document `kicad-cli` as a
command-line tool (subcommands `fp`, `jobset`, `pcb`, `sch`, `sym`,
`version`) but do **not** explicitly state it runs without a logged-in GUI
session; that claim is verified by use (it runs fine from a plain SSH shell —
this project already drives `kicad-cli` from terminals), not by a sentence in
the docs. If a macOS build ever demands a window server, run it from a
Screen-Sharing login session.

**The IPC API is a different story:** it is explicitly an interface for
"remotely controlling a running instance of KiCad" — the API server lives
inside the KiCad (GUI) process and speaks NNG over unix sockets
([KiCad IPC API docs](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/)).
No headless IPC server appears in the KiCad 10 CLI docs. So when
Konnect-on-Studio happens, the Studio needs a **logged-in GUI session with
KiCad open**: auto-login + Screen Sharing (prep steps 3/6 above), same
requirements as the Desktop-app option — do them together.

## Handoff workflow: laptop ↔ Studio

Division of labor:

| laptop (M4 Pro, at hand)                 | Studio (M4 Max, remote)                     |
|------------------------------------------|---------------------------------------------|
| design threads, architecture, review     | fleet benches (`run_bench.py --mode route`) |
| Konnect / KiCad GUI / Voxy iteration     | calibration sweeps (via_cost, dir_penalty)  |
| kernel dev + small-board correctness     | big-board routes, 0.25 mm fine-pitch runs   |
| writing/reading docs, quick experiments  | future 26M-node-class runs, soak jobs       |

Rules that make it work:

- **git is the interface between machines.** Laptop pushes a branch; the
  Studio session pulls, runs, commits results (e.g. `bench/results.json`),
  pushes back. Never point two writing sessions at one checkout — on the
  Studio, `claude remote-control --spawn worktree` enforces this per session.
- **Sessions don't share memory.** A Studio session knows nothing about the
  laptop conversation. Anything it needs must be in the repo (README,
  ARCHITECTURE.md, AGENTS.md, this file, commit messages) — which is a big
  part of why those files are as explicit as they are.
- **Name the machine.** Remote Control's default session names carry the
  hostname prefix; keep that (or pass `--name "Studio: fleet sweep"`), so the
  claude.ai/code session list tells you at a glance where each session runs.

A typical handoff, end to end:

1. Laptop session lands a router change, pushes branch `feat/x`.
2. From the laptop browser (or phone), open the Studio's Remote Control
   session at claude.ai/code.
3. Prompt: *"pull feat/x, re-run the fleet route bench, compare ratios
   against bench/results.json on main, report per-board deltas"*.
4. Close the laptop. The Studio grinds; the phone gets a push notification
   when it's done or blocked.
5. Studio session pushes `bench/results.json` on a results branch; laptop
   session reviews the diff in the morning.
