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
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if p.returncode != 0:
        return ""
    return p.stdout


def _size_bytes(value):
    """Parse a btrfs-style size token like '4.73TiB' / '512.00MiB' into bytes."""
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]*)", value or "")
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    mult = {
        "": 1, "B": 1,
        "K": 1000, "KB": 1000, "KIB": 1024,
        "M": 1000 ** 2, "MB": 1000 ** 2, "MIB": 1024 ** 2,
        "G": 1000 ** 3, "GB": 1000 ** 3, "GIB": 1024 ** 3,
        "T": 1000 ** 4, "TB": 1000 ** 4, "TIB": 1024 ** 4,
        "P": 1000 ** 5, "PB": 1000 ** 5, "PIB": 1024 ** 5,
    }
    return num * mult.get(m.group(2).upper(), 1)


def _md_sysfs(name):
    """Read md RAID state from /sys (readable without root, unlike mdadm --detail)."""
    base = "/sys/block/%s/md" % name
    res = {"degraded": None, "array_state": "", "failed_devices": 0,
           "spare_devices": 0, "used_devices": 0}
    dg = read_file(os.path.join(base, "degraded"))
    if dg is not None and dg.strip().isdigit():
        res["degraded"] = int(dg.strip())
    ast = read_file(os.path.join(base, "array_state"))
    if ast is not None:
        res["array_state"] = ast.strip()
    try:
        entries = os.listdir(base)
    except OSError:
        return res
    referenced = set()
    for e in entries:
        if re.match(r"^rd\d+$", e):
            try:
                referenced.add(os.path.basename(os.readlink(os.path.join(base, e))))
            except OSError:
                pass
    failed = spare = used = 0
    for e in entries:
        if not e.startswith("dev-"):
            continue
        st = (read_file(os.path.join(base, e, "state")) or "").strip().lower()
        if "faulty" in st:
            failed += 1
        elif e not in referenced:
            spare += 1
        else:
            used += 1
    res["failed_devices"] = failed
    res["spare_devices"] = spare
    res["used_devices"] = used
    return res


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
                elif "recovery" in line or "resync" in line or "check" in line:
                    pools[current]["resync"] = line.strip()
            continue
        m = re.match(r"^(\w+)\s*:\s*(\S+)?", line)
        if m and m.group(1) not in ("Personalities", "unused"):
            current = m.group(1)
            pools[current] = {"line": line, "blocks": "", "resync": ""}
        else:
            current = None

    out = {}
    for name, p in pools.items():
        first = p["line"]
        m = re.search(r"\[(\d+)/(\d+)\]", p["blocks"])
        required = active = None
        degraded = 0
        if m:
            # /proc/mdstat "[active/total]": group(1)=working, group(2)=required.
            active, required = int(m.group(1)), int(m.group(2))
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
        sysf = _md_sysfs(name)
        if sysf["degraded"]:
            degraded = 1
        if (state in ("", "unknown")) and sysf["array_state"]:
            state = sysf["array_state"]
        rec = {
            "state": "inactive" if state == "stopped" else state,
            "level": level,
            "degraded": degraded,
            "required_disks": required,
            "active_disks": active,
            "recovery_pct": recovery_pct,
            "failed_devices": sysf["failed_devices"],
            "spare_devices": sysf["spare_devices"],
            "used_devices": sysf["used_devices"],
        }
        out[name] = rec
    return out


def collect_zfs():
    zpool = "/usr/sbin/zpool" if os.path.exists("/usr/sbin/zpool") else "/usr/bin/zpool"
    if not os.path.exists(zpool):
        return {}
    listing = run([zpool, "list", "-H", "-o", "name,health"])
    out = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = {
                "state": parts[1].lower(),
                "state_ok": 1 if parts[1].lower() == "online" else 0,
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
            out[parts[0]] = {"uncorrectable": 0, "corrupted": 0, "used_pct": None}
    for dev in sorted(out):
        # device stats must be requested by device path; the mountpoint form
        # fails (Operation not permitted) unless run as root.
        stats = run([btrfs, "device", "stats", dev])
        uncorrectable = 0
        for mm in re.finditer(r"\.([A-Za-z_]+_io_errs)\s+(\d+)", stats):
            uncorrectable += int(mm.group(2))
        corrupted = 0
        for mm in re.finditer(r"\.corruption_errs\s+(\d+)", stats):
            if int(mm.group(1)) != 0:
                corrupted += 1
        out[dev]["uncorrectable"] = uncorrectable
        out[dev]["corrupted"] = corrupted
        mountpoint = None
        for mount_line in mounts.splitlines():
            parts = mount_line.split()
            if len(parts) >= 2 and parts[0] == dev:
                mountpoint = parts[1]
                break
        if mountpoint:
            usage = run([btrfs, "filesystem", "usage", mountpoint])
            dm = re.search(r"^\s*Device size:\s+([\d.]+\s*[A-Za-z]*)", usage, re.MULTILINE)
            um = re.search(r"^\s*Used:\s+([\d.]+\s*[A-Za-z]*)", usage, re.MULTILINE)
            total = _size_bytes(dm.group(1)) if dm else None
            used = _size_bytes(um.group(1)) if um else None
            if total and used is not None and total > 0:
                out[dev]["used_pct"] = used / total
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
