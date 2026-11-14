"""Shared collection logic for TA-zfspool (ZFS / md RAID / btrfs)."""
import json
import os
import re
import subprocess
import sys


def read_file(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def run(cmd, timeout=25):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def collect_md():
    mdstat = read_file("/proc/mdstat") or ""
    pools = {}
    current = None
    blocks_line = ""
    resync = ""
    for line in mdstat.splitlines():
        if line.startswith(" "):
            if current:
                if "blocks" in line:
                    pools[current]["blocks"] = line.strip()
                elif "recovery" in line or "resync" in line:
                    pools[current]["resync"] = line.strip()
            continue
        m = re.match(r"^(\w+)\s*:\s*(\S+)?", line)
        if m and m.group(1) not in ("Personalities", "unused"):
            current = m.group(1)
            pools[current] = {"line": line, "blocks": "", "resync": ""}
        else:
            current = None

    out = {}
    has_mdadm = os.path.exists("/usr/bin/mdadm") or os.path.exists("/usr/sbin/mdadm")
    for name, p in pools.items():
        first = p["line"]
        m = re.search(r"\[(\d+)/(\d+)\]", p["blocks"])
        required = active = None
        degraded = 0
        if m:
            required, active = int(m.group(1)), int(m.group(2))
            if required > active:
                degraded = 1
        layout = re.search(r"\[([U_]+)\]", p["blocks"])
        if layout and layout.group(1).count("_") > 0:
            degraded = 1
        recovery_pct = None
        rm = re.search(r"=\s*([\d.]+)%", p["resync"])
        if rm:
            recovery_pct = float(rm.group(1))
        tokens = first.split()
        if "level=" in p["blocks"]:
            level = p["blocks"].split("level=")[-1].split()[0].rstrip(",")
        elif len(tokens) >= 4:
            level = tokens[3]
        else:
            level = ""
        state = tokens[2] if len(tokens) >= 3 else "unknown"
        rec = {
            "state": "inactive" if state == "stopped" else state,
            "level": level,
            "degraded": degraded,
            "required_disks": required,
            "active_disks": active,
            "recovery_pct": recovery_pct,
        }
        if has_mdadm:
            detail = run(["mdadm", "--detail", "/dev/%s" % name])
            for label, key in (
                ("Failed Devices", "failed_devices"),
                ("Spare Devices", "spare_devices"),
                ("Used Devices", "used_devices"),
            ):
                dm = re.search(r"%s\s*:\s*(\d+)" % label, detail)
                if dm:
                    rec[key] = int(dm.group(1))
            if rec.get("failed_devices") and not degraded:
                rec["degraded"] = 1
        out[name] = rec
    return out


def collect_zfs():
    zpool = "/usr/sbin/zpool" if os.path.exists("/usr/sbin/zpool") else "/usr/bin/zpool"
    if not os.path.exists(zpool):
        return {}
    listing = run([zpool, "list", "-H", "-o", "name,state,scan"])
    out = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = {
                "state": parts[1].lower(),
                "state_ok": 1 if parts[1].lower() == "on" else 0,
                "failed_vdevs": 0,
                "scrub_pct": None,
            }
    if not out:
        return out
    status = run([zpool, "status"])
    pool = None
    for line in status.splitlines():
        m = re.match(r"^\s*(?:pool|Pool):\s*(\S+)", line)
        if m:
            pool = m.group(1)
            if pool not in out:
                out[pool] = {"state": "unknown", "state_ok": 1, "failed_vdevs": 0, "scrub_pct": None}
            continue
        m2 = re.search(r"scan:\s*(?:scrub|rescan|recover)\s+in progress[^\n%]*?([\d.]+)%", line)
        if m2 and pool and pool in out:
            out[pool]["scrub_pct"] = float(m2.group(1))
        stripped = line.lstrip()
        if pool and pool in out and re.match(r"(?:FAILED|FAULTED|OFFLINE|UNAVAIL)\b", stripped):
            out[pool]["failed_vdevs"] += 1
    return out


def collect_btrfs():
    btrfs = None
    for c in ("btrfs",):
        if os.path.exists("/usr/sbin/%s" % c) or os.path.exists("/usr/bin/%s" % c):
            btrfs = c
            break
    if not btrfs:
        return {}
    mounts = read_file("/proc/mounts") or ""
    out = {}
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "btrfs":
            out[parts[0]] = {"uncorrectable": None, "corrupted": None}
    for dev in sorted(out):
        stats = run([btrfs, "device", "stats", dev])
        mu = re.search(r"Uncorrectable errors:\s*(\d+)", stats)
        mc = re.search(r"Corrupted metadata:\s*(\d+)", stats)
        if mu:
            out[dev]["uncorrectable"] = int(mu.group(1))
        if mc:
            out[dev]["corrupted"] = int(mc.group(1))
        for mount_line in mounts.splitlines():
            parts = mount_line.split()
            if len(parts) >= 2 and parts[0] == dev:
                usage = run([btrfs, "filesystem", "usage", parts[1]])
                used = re.search(r"Used Data:\s*([\d.]+)", usage)
                if used:
                    out[dev]["used_pct"] = float(used.group(1).rstrip("%")) / 100.0
                break
    return out


def discover():
    md = collect_md()
    zfs = collect_zfs()
    btrfs = collect_btrfs()
    present = [n for n, m in (("md", md), ("zfs", zfs), ("btrfs", btrfs)) if m]
    status = "ok"
    for d in md.values():
        if d.get("degraded"):
            status = "alarm"
    for p in zfs.values():
        if p.get("state_ok") == 0:
            status = "alarm"
    for b in btrfs.values():
        if (b.get("uncorrectable") or 0) > 0 or (b.get("corrupted") or 0) > 0:
            status = "alarm"
    return {
        "status": status,
        "subsystems_present": present,
        "md": md,
        "zfs": zfs,
        "btrfs": btrfs,
    }


def metrics_payload(data):
    payload = {
        "pools_total": len(data.get("md", {})) + len(data.get("zfs", {})) + len(data.get("btrfs", {})),
        "subsystems": len(data.get("subsystems_present", [])),
    }
    for name, d in data.get("md", {}).items():
        for key in ("degraded", "required_disks", "active_disks", "failed_devices", "spare_devices", "recovery_pct"):
            if d.get(key) is not None:
                payload["md_%s_%s" % (name, key)] = int(d[key]) if float(d[key]).is_integer() else float(d[key])
    for name, d in data.get("zfs", {}).items():
        for key in ("state_ok", "failed_vdevs", "scrub_pct"):
            if d.get(key) is not None:
                payload["zfs_%s_%s" % (name, key)] = int(d[key]) if float(d[key]).is_integer() else float(d[key])
    for name, d in data.get("btrfs", {}).items():
        for key in ("uncorrectable", "corrupted", "used_pct"):
            if d.get(key) is not None:
                payload["btrfs_%s_%s" % (name, key)] = d[key]
    return payload


def emit(payload):
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def fail(message):
    sys.stderr.write("TA-zfspool collection error: %s\n" % message)
    return 1
