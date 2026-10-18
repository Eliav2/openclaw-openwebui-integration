#!/usr/bin/env python3
"""Drop the Pipe's Gateway connections from outside, as a network drop does.
Test tooling only (the Pipe has no drop hook of its own since ELI-145).

How: the Open WebUI container's processes are found with `docker top`; in
their network namespace (`nsenter -n`), every established TCP connection to
the Gateway port that one of them holds is shut down through a copy of its fd
(`pidfd_getfd`, then `shutdown(SHUT_RDWR)`). The copy is the same kernel
socket, so the Pipe's reader sees the connection end at once, with no close
frame (code 1006), and the Gateway process is never touched: its run goes on
and the Pipe reconnects to the same boot. Needs root (pidfd_getfd needs
CAP_SYS_PTRACE over the target): it re-runs itself with `sudo -n` otherwise,
as on a CI runner.

usage: socket_drop.py [CONTAINER]     (default $E2E_OWUI or "owui"; prints JSON)
       from socket_drop import drop; drop()  -> the connections shut down
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

GATEWAY_PORT = 18789

# Runs in the container's network namespace: /proc/net/tcp is read there.
_HELPER = r'''
import ctypes, json, os, socket, sys
port, pids = int(sys.argv[1]), set(sys.argv[2].split(","))
conns = {}
for table in ("tcp", "tcp6"):   # "localhost" can be ::1
    for line in open("/proc/net/" + table).read().splitlines()[1:]:
        f = line.split()
        if f[3] == "01" and int(f[2].split(":")[1], 16) == port and f[9] != "0":
            conns[f[9]] = "%s :%d->:%d" % (table, int(f[1].split(":")[1], 16), port)
libc, done, out = ctypes.CDLL(None, use_errno=True), set(), []
for pid in sorted(pids):
    try:
        fds = os.listdir("/proc/%s/fd" % pid)
    except OSError:
        continue
    for fd in fds:
        try:
            link = os.readlink("/proc/%s/fd/%s" % (pid, fd))
        except OSError:
            continue
        ino = link[8:-1] if link.startswith("socket:[") else None
        if ino not in conns or ino in done:
            continue
        pidfd = libc.syscall(434, int(pid), 0)                              # pidfd_open
        dup = libc.syscall(438, pidfd, int(fd), 0) if pidfd >= 0 else -1    # pidfd_getfd
        if dup < 0:
            out.append({"conn": conns[ino], "error": os.strerror(ctypes.get_errno())})
            continue
        socket.socket(fileno=dup).shutdown(socket.SHUT_RDWR)
        os.close(pidfd)
        done.add(ino)
        out.append({"conn": conns[ino], "pid": int(pid), "fd": int(fd)})
print(json.dumps(out))
'''


def drop(container: str | None = None, port: int = GATEWAY_PORT) -> list:
    """Shut down the Gateway connections of `container`'s processes. Returns
    one {conn, pid, fd} per connection shut down ({conn, error} on failure)."""
    container = container or os.environ.get("E2E_OWUI") or "owui"
    if os.geteuid() != 0:
        r = subprocess.run(["sudo", "-n", "-E", sys.executable, os.path.abspath(__file__), container],
                           capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise RuntimeError(f"socket drop failed: {r.stderr.strip()[-400:]}")
        return json.loads(r.stdout.strip().splitlines()[-1])
    top = subprocess.run(["docker", "top", container, "-eo", "pid"], capture_output=True,
                         text=True, timeout=30, check=True).stdout.split()[1:]
    pids = [p for p in top if p.isdigit()]
    if not pids:
        raise RuntimeError(f"no processes in container {container!r}")
    r = subprocess.run(["nsenter", "-t", pids[0], "-n", sys.executable, "-", str(port), ",".join(pids)],
                       input=_HELPER, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(f"socket drop helper failed: {r.stderr.strip()[-400:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


if __name__ == "__main__":
    print(json.dumps(drop(sys.argv[1] if len(sys.argv) > 1 else None)))
