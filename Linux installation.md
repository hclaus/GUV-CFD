# WSL + OpenFOAM + SSH setup for GUV-CFD

This machine's actual setup (confirmed 2026-08-06): WSL2 running **Ubuntu
24.04.1 LTS (Noble Numbat)**, distro name `Ubuntu`, Linux user `hclaus`
(UID 1000), OpenFOAM **v2412** installed via the official OpenCFD
(openfoam.com) apt repository. Part 1 below is the from-scratch sequence
to reproduce that on a brand-new machine; Part 2 is the SSH/paramiko
transport setup this app also uses (see "Why" under Part 2).

## Part 1 - WSL2 + Ubuntu + OpenFOAM from scratch

### 1a. Install WSL2 and Ubuntu

From an **elevated** (Run as Administrator) Command Prompt or PowerShell:

```
wsl --install
```

On modern Windows 10 (2004+) / Windows 11 this single command enables the
required Windows features (WSL, Virtual Machine Platform), downloads the
Linux kernel, and installs Ubuntu (the default distro) - all in one step.
**Reboot when it asks you to.**

If you specifically want Ubuntu 24.04 rather than whatever the current
default happens to be:

```
wsl --install -d Ubuntu-24.04
```

Check what's installed at any point with:

```
wsl -l -v
```

(should show `Ubuntu`, `Running` or `Stopped`, `VERSION 2` - if it shows
version 1, run `wsl --set-version Ubuntu 2`, then `wsl --set-default-version 2`
for future installs).

### 1b. First launch - create your Linux username/password (interactive)

Launch it once (Start menu -> "Ubuntu", or type `wsl` in a terminal). The
**very first launch** runs an interactive setup wizard that asks you to
choose a **UNIX username** and **password** for this distro - this is a
completely separate account from your Windows login, and there's no
command-line way to skip or pre-script this step; just answer the two
prompts. This becomes the distro's default user (confirmed here:
`hclaus`, UID 1000) and the one you `sudo` as afterward.

### 1c. Enable systemd (needed later for auto-starting sshd)

From inside WSL (`wsl`, or open the Ubuntu app):

```bash
sudo nano /etc/wsl.conf
```

Add (or confirm) these lines, save, and exit (`Ctrl+O`, Enter, `Ctrl+X`
in nano):

```ini
[boot]
systemd=true
```

Then from Windows, restart WSL for this to take effect:

```
wsl --shutdown
```

(Just launching `wsl` again afterward restarts it.) Confirmed here:
`systemctl --version` shows systemd running as PID 1 once this is set.

### 1d. Update the base system

```bash
sudo apt update && sudo apt upgrade -y
```

### 1e. Install OpenFOAM v2412 (official OpenCFD apt repo)

This is the exact method already used on this machine - the official
one-liner from openfoam.com's own Debian/Ubuntu install instructions:

```bash
curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash
sudo apt-get update
sudo apt-get install -y openfoam2412-default
```

This registers `deb [arch=amd64] https://dl.openfoam.com/repos/deb noble main`
in `/etc/apt/sources.list.d/openfoam.list`, imports OpenCFD's GPG signing
key, then installs the "everything" package (runtime + dev headers + build
tools + tutorials - `openfoam2412-default` pulls in
`openfoam2412`/`openfoam2412-dev`/`openfoam2412-tools`/etc. as
dependencies, confirmed via `dpkg -l | grep openfoam` on this machine).

Verify it installed correctly:

```bash
source /usr/lib/openfoam/openfoam2412/etc/bashrc
blockMesh -help
```

`blockMesh -help` should print its usage text, not "command not found".
(This app never relies on OpenFOAM being sourced in `~/.bashrc` - every
WSL command it runs sources `/usr/lib/openfoam/openfoam2412/etc/bashrc`
explicitly first, matching `guvcfd/wsl_utils.py`'s `OPENFOAM_BASHRC`
constant - so this manual `source` step is only to verify the install,
not something you need to keep doing yourself.)

### 1f. Optional but recommended - give WSL more resources for real CFD cases

By default WSL2 caps itself to a fraction of your machine's RAM/CPUs
(with no explicit override, roughly half of total host RAM) and a small
default swap - fine for a quick test, but a real sweep can exceed it. On
the **Windows** side, create/edit `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
memory=16GB
processors=8
```

(adjust `memory`/`processors` to your machine - leave enough headroom for
Windows itself) then `wsl --shutdown` and relaunch for it to take effect.
Not required to get started, but worth doing before a real production
sweep.

**This machine's actual setup (confirmed 2026-08-18)**, on a 16GB-RAM
host:

```ini
[wsl2]
vmIdleTimeout=-1
memory=10GB
swap=4GB
```

`vmIdleTimeout=-1` disables WSL's idle-shutdown timer (needed for the SSH
transport in Part 2 to survive a quiet period without WSL tearing itself
down mid-connection). `memory`/`swap` were raised after a real incident:
running two concurrent flow-base builds on a fine (0.08m) mesh exceeded
WSL's un-overridden default ceiling (~7.6GB) and crashed the whole WSL2
VM outright - confirmed via Windows' own Hyper-V event log showing the
WSL VM's network adapter torn down and recreated mid-solve, and both
`log.simpleFoam` files stopping abruptly with no FOAM error (the solves
themselves were converging cleanly right up to the last line - this
was a VM-level OOM, not a numerical divergence). `wsl --shutdown` +
relaunch is required for any `.wslconfig` change to take effect, and it
kills anything currently running in WSL - check for running solves
first (`wsl -e bash -lc "ps aux | grep -i foam"`).

### 1g. Sanity check before moving to Part 2

At this point you should be able to, from Windows:

```
wsl -e bash -lc "source /usr/lib/openfoam/openfoam2412/etc/bashrc && blockMesh -help"
```

and see the same usage text as in 1e, confirming Windows can already
drive WSL/OpenFOAM via the existing `wsl.exe` subprocess mechanism this
app's default transport uses - Part 2 below is what adds the *optional*,
faster SSH transport on top of that, not a replacement prerequisite.

---

## Part 2 - SSH (paramiko) transport setup

Why: replaces per-command `wsl.exe` subprocess spawning + manual `cat`-piped
file I/O with a persistent SSH connection (paramiko) into WSL - fixes both
`wsl.exe` launch flakiness and the Windows<->WSL cross-boundary filesystem
consistency bugs documented in this session's memory
(`project_wsl_cross_boundary_write_bug_2026-08-03`). See the plan file
`dapper-wobbling-clover.md` for the full design/rationale. **This part is
optional** - the app works fine on the default `subprocess` transport set
up by Part 1 alone; skip to here only if you want to try the `ssh`
transport (`GUVCFD_WSL_TRANSPORT=ssh`).

### Step 1 - install and enable openssh-server inside WSL (needs sudo)

Run these from a Windows Command Prompt:

```
wsl
```

This drops you into the WSL Linux shell. Then, one at a time (each `sudo`
line will prompt for your Linux account password):

```bash
sudo apt update
sudo apt install -y openssh-server
sudo systemctl enable --now ssh
sudo systemctl status ssh --no-pager
```

The last command must show `active (running)`. Confirmed working
2026-08-06 - `sshd` active, listening on `0.0.0.0:22` and `[::]:22`.

Type `exit` to leave WSL and return to the normal Windows prompt.

**Note**: `systemd=true` (set in Part 1c) is what makes `systemctl enable`
actually persist ssh across WSL restarts/reboots - without that setting,
this whole approach wouldn't survive a `wsl --shutdown`.

### Step 2 - dedicated SSH keypair (no sudo needed, done automatically)

A keypair dedicated to this integration (not the user's personal SSH key)
was generated on the Windows side:

```
ssh-keygen -t ed25519 -f ~/.ssh/guvcfd_wsl_key -N "" -C "guvcfd-paramiko"
```

- Private key: `C:\Users\hukcl\.ssh\guvcfd_wsl_key` (Windows side - this is
  what the Python/paramiko code reads)
- Public key: `C:\Users\hukcl\.ssh\guvcfd_wsl_key.pub`

The public key was appended to the WSL user's `authorized_keys`:

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
echo "<contents of guvcfd_wsl_key.pub>" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

(WSL user: `hclaus`. No `~/.ssh` directory existed there before this.)

### Step 3 - verify passwordless connectivity

WSL uses standard NAT networking (not mirrored mode), so its IP address
can change across `wsl --shutdown`/restart cycles - resolve it fresh each
time rather than hardcoding it:

```bash
wsl -e hostname -I
```

(returned `172.30.17.121` at setup time - expect this to differ after a
WSL restart).

Then test the actual connection:

```bash
ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes \
    -i ~/.ssh/guvcfd_wsl_key hclaus@<the-ip-from-above> \
    "echo CONNECTED as \$(whoami) from \$(hostname)"
```

Confirmed working 2026-08-06 - connected with zero prompts, printed
`CONNECTED as hclaus from HolgersLT`.

### Step 4 - raise sshd's MaxSessions cap (needed for real concurrent sweeps)

Why: `wsl_utils.py` uses ONE shared SSH `Transport` app-wide; a real sweep
opens up to 9 concurrent channels on it at once (persistent SFTP per
thread, plus short exec channels for `mkdir`/`ls`/`cp -r`/`rm -rf`, etc.
- see `scenario_runs._MAX_CONCURRENT_SOLVES`). OpenSSH's own default
`MaxSessions 10` (the cap on channels open *at once* per connection) is
close enough to that to fail intermittently under real load - confirmed
directly: a 6-9 thread stress test
(`tests/test_ssh_transport_concurrency.py`) failed 83-85/100 attempts at
the default, and 0/100 after this fix (see
`project_ssh_stress_test_pending_2026-08-09` memory for the full
before/after numbers).

From inside WSL (`wsl`, one `sudo` command - will prompt for your Linux
account password):

```bash
sudo sed -i 's/^#MaxSessions 10/MaxSessions 40/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

Verify it took effect:

```bash
grep MaxSessions /etc/ssh/sshd_config
```

should print `MaxSessions 40`. The app's own client-side channel
semaphore (`_ssh_channel_semaphore` in `wsl_utils.py`) is set to 30,
comfortably under this 40 - if you raise `MaxSessions` further, there's
no need to also raise the semaphore unless you also raise
`_MAX_CONCURRENT_SOLVES` well past 9.

## Code side (tracked in the plan file)

`guvcfd/wsl_utils.py` has a paramiko-based connection manager and SSH/SFTP
implementations of `run_wsl`/`run_wsl_or_raise`/`run_wsl_streaming`/
`write_wsl_text`/`read_wsl_text`, selectable via a `GUVCFD_WSL_TRANSPORT`
env var (`subprocess` = the default, Part 1's mechanism; `ssh` = Part 2's
new path) - see the plan file `dapper-wobbling-clover.md` for the full
phased rollout and verification. Nothing past Part 2 Step 3 needs manual
WSL-side setup again; if WSL is ever reset/rebuilt, redo Part 1 and Part 2
Steps 1-3 first.

## How to actually turn SSH mode on (no command line needed)

**By default, nothing changes** - the app talks to WSL exactly the same
way it always has, even after everything in Part 2 above is set up.
SSH mode is only used if you explicitly ask for it.

An "environment variable" here is just a named setting the app checks
when it starts: is `GUVCFD_WSL_TRANSPORT` set, and if so, to what? If
it's not set at all (the normal case), the app uses the old method. Set
it to `ssh` and it uses the new one instead. It only affects the one
program launched right after setting it - it doesn't change anything
permanently, and doesn't affect any other window or program.

**The easy way - dedicated launch files** (no editing, no typing
commands): alongside the normal launcher, there's a second one that
already has this switched on:

- Normal (unchanged): `StartPCApp.bat` (native app) /
  `start_server.bat` (browser app)
- SSH mode: `StartPCApp_SSH.bat` (native app) /
  `start_server_SSH.bat` (browser app)

Just double-click whichever one you want to use. There's nothing else to
set up or remember - want to go back to normal, double-click the plain
one next time.

(If you ever do want to set it manually from a Command Prompt instead:
`set GUVCFD_WSL_TRANSPORT=ssh` in that window, then launch the app from
the *same* window - but the `.bat` files above already do this for you,
so there's no need to.)

## Troubleshooting

- If `wsl -e hostname -I` returns a different IP than expected, that's
  normal after a WSL restart - the connection code re-resolves it, no
  action needed.
- If `sudo systemctl status ssh` ever shows `inactive`/`failed` after a
  reboot despite `systemd=true`, run `sudo systemctl enable --now ssh`
  again - the WSL VM only starts systemd once WSL itself launches, and a
  cold boot occasionally needs the service explicitly (re-)started.
- If SSH connection is refused entirely, check Windows Firewall isn't
  blocking the WSL virtual network adapter (uncommon, but possible after
  a Windows Update).
- If SSH mode fails intermittently only under real concurrent-sweep load
  (`Secsh channel N open FAILED: open failed: Connect failed`, or
  SFTP reads/writes failing sporadically), check Part 2 Step 4's
  `MaxSessions` setting hasn't reverted to OpenSSH's default of 10 - this
  is the single most likely cause, not a paramiko/thread-safety bug.
- If `blockMesh -help` (Part 1e) says "command not found" even after
  sourcing the bashrc, double check `openfoam2412-default` actually
  installed (`dpkg -l | grep openfoam2412` should list several packages)
  rather than just `openfoam2412` alone - the "-default" meta-package is
  what pulls in the dev/tools components this app's mesh/topoSet/etc.
  calls need.
