#!/usr/bin/env python3
"""
Deadline controller for a rented GPU box (vast.ai).

  python deadline.py daemon          background watchdog (run in tmux; one instance only)
  python deadline.py status          print status
  python deadline.py selftest        read-only check that the per-instance vast.ai key works
  python deadline.py set MIN         arm the deadline MIN minutes from now
  python deadline.py extend MIN      extend the deadline
  python deadline.py end-now         final sync + action immediately

What the daemon does:
  T-ask_lead    raises a QUESTION ("final stretch or extend?") that the MCP tools surface to Claude.
                No reply within the window -> continues to the deadline as planned.
  T-final_lead  creates DIR/SAVE_NOW (training should checkpoint and delete it), waits a grace
                period, then runs the sync command(s) (retries x3).
  T-0           applies ON_DEADLINE: stop | destroy | none, using the per-instance key
                (CONTAINER_API_KEY / CONTAINER_ID, which can only control THIS instance).
                If the final sync failed, destroy degrades to stop so nothing is lost; if the destroy
                call itself fails it falls back to stop, so the instance never keeps billing.

State lives in DIR/state.json (default /workspace/.deadline). The MCP server imports register()
and banner() from this file; the daemon is a separate process so it survives server restarts.

Env (read when the state file is first created):
  DEADLINE_MIN / DEADLINE_TS   initial deadline (minutes from now / epoch seconds)
  ON_DEADLINE=destroy|stop|none (default destroy)   ASK_LEAD_MIN=15  FINAL_LEAD_MIN=8
  REPLY_WINDOW_SEC=300         SAVE_GRACE_SEC=60
  CKPT_REPO, CKPT_DIR=/workspace/checkpoints, KEEP=3   -> default sync = hf_sync.py push
  SYNC_CMD                     override the sync command; CODE_SYNC_CMD  optional extra command
  VAST_READ_KEY                optional scoped read-only key to show credit balance
  VAST_API_BASE                override API base (testing)
"""
import asyncio
import contextlib
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DIR = Path(os.environ.get("DEADLINE_DIR", "/workspace/.deadline"))
STATE, LOCK, LOG = DIR / "state.json", DIR / ".lock", DIR / "log.txt"
SAVE_NOW, DAEMON_LOCK = DIR / "SAVE_NOW", DIR / ".daemon.lock"
HERE = Path(__file__).resolve().parent
API = os.environ.get("VAST_API_BASE", "https://console.vast.ai/api/v0").rstrip("/")


# ───────────────────────── helpers ─────────────────────────
def _env(name, default=None):
    """os.environ first, then /etc/environment (SSH shells often lack container vars)."""
    v = os.environ.get(name)
    if v:
        return v
    try:
        for line in Path("/etc/environment").read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return default


def _hhmm(ts):
    return time.strftime("%H:%M", time.gmtime(ts)) + " UTC"


def log(msg):
    DIR.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%F %T', time.gmtime())}Z {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


@contextlib.contextmanager
def locked():
    DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _cfg():
    f = lambda n, d: float(os.environ.get(n, d))
    return {
        "on_deadline": os.environ.get("ON_DEADLINE", "destroy"),
        "ask_lead_min": f("ASK_LEAD_MIN", 15),
        "final_lead_min": f("FINAL_LEAD_MIN", 8),
        "reply_window_sec": f("REPLY_WINDOW_SEC", 300),
        "save_grace_sec": f("SAVE_GRACE_SEC", 60),
    }


def fresh():
    s = {
        "deadline_ts": None, "config": _cfg(),
        "asked_for": None, "asked_at": None, "question": None, "reply_by": None, "answer": None,
        "final_synced_for": None, "final_sync": None, "sync_requested": False, "syncing": False,
        "acted_for": None, "action": None, "extensions": [],
        "control": {"ok": None, "detail": "not checked yet"},
        "rate_per_hour": None, "hard_end_ts": None,
    }
    if os.environ.get("DEADLINE_TS"):
        s["deadline_ts"] = float(os.environ["DEADLINE_TS"])
    elif os.environ.get("DEADLINE_MIN"):
        s["deadline_ts"] = time.time() + float(os.environ["DEADLINE_MIN"]) * 60
    return s


def load():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return fresh()


def save(s):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    os.replace(tmp, STATE)


# ───────────────────────── vast.ai (per-instance key) ─────────────────────────
def vast_request(method, body=None, timeout=20):
    iid, key = _env("CONTAINER_ID"), _env("CONTAINER_API_KEY")
    if not iid or not key:
        raise RuntimeError("CONTAINER_ID / CONTAINER_API_KEY not found in environment")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}/instances/{iid}/", data=data, method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode() or "{}"
    return json.loads(txt)


def instance_info():
    """Read-only: returns dict(ok, detail, rate, end)."""
    try:
        d = vast_request("GET")
        inst = d.get("instances", d) if isinstance(d, dict) else d
        if isinstance(inst, list):
            inst = inst[0] if inst else {}
        rate, end = inst.get("dph_total"), inst.get("end_date")
        return {"ok": True, "detail": f"reachable, status={inst.get('actual_status')}",
                "rate": rate if isinstance(rate, (int, float)) else None,
                "end": end if isinstance(end, (int, float)) and end > 1e9 else None}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}", "rate": None, "end": None}


def vast_action(kind):
    """kind: 'stop' | 'destroy'. REST first, `vastai` CLI as fallback."""
    try:
        vast_request("PUT", {"state": "stopped"}) if kind == "stop" else vast_request("DELETE")
        return True, "REST ok"
    except Exception as e:
        err = f"REST failed: {type(e).__name__}: {e}"
    if shutil.which("vastai"):
        r = subprocess.run(["vastai", kind, "instance", _env("CONTAINER_ID", ""),
                            "--api-key", _env("CONTAINER_API_KEY", "")],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return True, f"{err}; CLI ok"
        err += f"; CLI rc={r.returncode} {r.stderr[-150:]}"
    return False, err


def get_credit():
    """Optional: needs VAST_READ_KEY (scoped read-only key) + `pip install vastai`."""
    key = _env("VAST_READ_KEY")
    if not key:
        return None
    try:
        from vastai import VastAI
        u = VastAI(api_key=key).show_user()
        if isinstance(u, str):
            u = json.loads(u)
        for k in ("credit", "balance"):
            if isinstance(u, dict) and isinstance(u.get(k), (int, float)):
                return float(u[k])
    except Exception:
        pass
    return None


# ───────────────────────── final sync ─────────────────────────
def sync_commands():
    cmds = []
    if os.environ.get("SYNC_CMD"):
        cmds.append(os.environ["SYNC_CMD"])
    elif os.environ.get("CKPT_REPO"):
        d = os.environ.get("CKPT_DIR", "/workspace/checkpoints")
        cmds.append(f"python3 {shlex.quote(str(HERE / 'hf_sync.py'))} push {shlex.quote(d)} "
                    f"{shlex.quote(os.environ['CKPT_REPO'])} --type model "
                    f"--keep {int(os.environ.get('KEEP', '3'))}")
    if os.environ.get("CODE_SYNC_CMD"):
        cmds.append(os.environ["CODE_SYNC_CMD"])
    return cmds


def run_final_sync(cfg):
    log("final sync: requesting a checkpoint via SAVE_NOW")
    SAVE_NOW.write_text(str(time.time()))
    end = time.time() + cfg["save_grace_sec"]
    while time.time() < end and SAVE_NOW.exists():
        time.sleep(1)
    log("SAVE_NOW acknowledged" if not SAVE_NOW.exists() else "SAVE_NOW not acknowledged, continuing")
    cmds = sync_commands()
    if not cmds:
        return {"ok": None, "at": time.time(), "detail": "no sync command configured"}
    env = dict(os.environ)
    if "HF_TOKEN" not in env and _env("HF_TOKEN"):
        env["HF_TOKEN"] = _env("HF_TOKEN")
    detail = []
    for cmd in cmds:
        ok = False
        for attempt in range(1, 4):
            try:
                r = subprocess.run(cmd, shell=True, env=env, capture_output=True, text=True,
                                   timeout=1800)
                tail = (r.stdout + r.stderr).strip()[-200:]
                ok = r.returncode == 0
            except Exception as e:
                tail, ok = repr(e), False
            log(f"sync attempt {attempt} rc_ok={ok}: {cmd[:60]}")
            if ok:
                break
            time.sleep(10)
        detail.append(f"{'ok' if ok else 'FAILED'}: {cmd[:60]} {'' if ok else tail}")
        if not ok:
            return {"ok": False, "at": time.time(), "detail": " | ".join(detail)}
    return {"ok": True, "at": time.time(), "detail": " | ".join(detail)}


# ───────────────────────── daemon ─────────────────────────
def tick():
    now = time.time()
    run_sync = False
    with locked():
        s = load()
        cfg, dl = s["config"], s["deadline_ts"]
        if dl is None:
            return
        left = dl - now
        ask_lead, final_lead = cfg["ask_lead_min"] * 60, cfg["final_lead_min"] * 60
        dirty = False
        if s["asked_for"] != dl and 0 < left <= ask_lead:
            s.update(asked_for=dl, asked_at=now, answer=None,
                     reply_by=min(now + cfg["reply_window_sec"], max(now + 1, dl - final_lead)),
                     question="Deadline is approaching: ask the user whether this is the final "
                              "stretch or whether to extend (and by how long).")
            log(f"QUESTION raised ({left / 60:.1f} min left)")
            dirty = True
        if s["question"] and now >= s["reply_by"]:
            s["question"], s["answer"] = None, "no reply in time -> continuing to the deadline as planned"
            log("question window expired, continuing as planned")
            dirty = True
        mark = left <= final_lead
        if s["acted_for"] != dl and (s["sync_requested"] or (mark and s["final_synced_for"] != dl)):
            run_sync, sync_for = True, dl
            s["syncing"] = True
            dirty = True
        if dirty:
            save(s)
    if run_sync:
        res = run_final_sync(cfg)
        with locked():
            s = load()
            s.update(final_sync=res, sync_requested=False, syncing=False)
            if mark and s["deadline_ts"] == sync_for:
                s["final_synced_for"] = sync_for
            save(s)
        log(f"final sync result: ok={res['ok']} {res['detail'][:120]}")
    with locked():
        s = load()
        dl = s["deadline_ts"]
        if (dl is not None and dl - time.time() <= 0 and s["acted_for"] != dl
                and s["final_synced_for"] == dl):
            pol = s["config"]["on_deadline"]
            synced_ok = bool(s["final_sync"] and s["final_sync"]["ok"] is True)
            if pol == "none":
                s["action"] = {"kind": "none", "ok": True, "detail": "policy none: left running"}
            else:
                kind = pol
                if kind == "destroy" and not synced_ok:
                    kind = "stop"
                    log("final sync not confirmed -> STOP instead of destroy (data kept)")
                ok, detail = vast_action(kind)
                if not ok and kind == "destroy":
                    log(f"destroy failed ({detail[:120]}) -> trying STOP instead")
                    ok2, detail2 = vast_action("stop")
                    kind, ok, detail = "stop", ok2, f"destroy failed, fell back to stop: {detail2}"
                s["action"] = {"kind": kind, "ok": ok, "detail": detail, "at": time.time()}
                log(f"deadline action {kind}: ok={ok} {detail}")
            s["acted_for"] = dl
            save(s)


def daemon():
    DIR.mkdir(parents=True, exist_ok=True)
    lock_fh = open(DAEMON_LOCK, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("another deadline daemon is already running")
    with locked():
        if not STATE.exists() or os.environ.get("DEADLINE_RESET") == "1":
            save(fresh())
    log("daemon started")
    last_info = 0.0
    while True:
        try:
            if time.time() - last_info > 300:
                last_info = time.time()
                info = instance_info()
                with locked():
                    s = load()
                    s["control"] = {"ok": info["ok"], "detail": info["detail"]}
                    if info["rate"] is not None:
                        s["rate_per_hour"] = info["rate"]
                    if info["end"] is not None:
                        s["hard_end_ts"] = info["end"]
                    save(s)
                if not info["ok"]:
                    log(f"vast control check FAILED: {info['detail']}")
            tick()
        except Exception as e:
            log(f"tick error: {e!r}")
        time.sleep(5)


# ───────────────────────── status / actions (shared by CLI and MCP) ─────────────────────────
def summarize(s, with_credit=False):
    now, cfg, dl = time.time(), s["config"], s["deadline_ts"]
    if dl is None:
        return "No deadline armed. Use deadline_set(minutes) to arm one."
    left = (dl - now) / 60
    out = []
    if s["acted_for"] == dl:
        out.append(f"DEADLINE EXECUTED ({_hhmm(dl)}): {json.dumps(s['action'])}")
    elif left <= 0:
        out.append(f"PAST DEADLINE by {-left:.1f} min - final sync/action in progress")
    else:
        out.append(f"{left:.1f} min left (deadline {_hhmm(dl)}) | on deadline: {cfg['on_deadline']} "
                   f"| final sync starts {cfg['final_lead_min']:g} min before")
    if s["question"]:
        wait = max(0, (s["reply_by"] - now))
        out.append(f"PENDING QUESTION (auto-continues in {wait:.0f}s if unanswered): {s['question']}")
    elif s["answer"]:
        out.append(f"last decision: {s['answer']}")
    rate = s["rate_per_hour"]
    cost = (f"rate ${rate:.3f}/h -> +30 min ~ ${rate / 2:.2f}, +60 min ~ ${rate:.2f}"
            if rate else "rate unknown (instance info not readable)")
    out.append(cost)
    if with_credit:
        c = get_credit()
        if c is None:
            out.append("credit: unknown (needs VAST_READ_KEY)")
        else:
            hrs = f" (~{c / rate:.1f} h at current rate)" if rate else ""
            out.append(f"credit: ${c:.2f}{hrs}")
    fs = s["final_sync"]
    if s["syncing"]:
        out.append("final sync: IN PROGRESS")
    elif fs:
        out.append(f"last final sync: ok={fs['ok']} at {_hhmm(fs['at'])} - {fs['detail'][:150]}")
    c = s["control"]
    out.append(f"vast control: {'OK' if c['ok'] else 'NOT OK' if c['ok'] is False else '?'} ({c['detail'][:120]})")
    if s["hard_end_ts"]:
        out.append(f"rental hard end: {_hhmm(s['hard_end_ts'])} (cannot be extended past this)")
    return "\n".join(out)


def banner():
    """Short text appended to MCP tool results when the deadline is near or a question is pending."""
    try:
        s = json.loads(STATE.read_text())
        dl = s.get("deadline_ts")
        if dl is None or s.get("acted_for") == dl:
            return ""
        left = (dl - time.time()) / 60
        if s.get("question"):
            return (f"\n\n[DEADLINE {left:.0f} min - QUESTION PENDING: ask the user now whether this is "
                    f"the final stretch or to extend, then call deadline_extend / deadline_keep / "
                    f"deadline_end_now. time_left has details.]")
        if left <= max(s["config"]["ask_lead_min"], 20):
            return f"\n\n[DEADLINE: {left:.0f} min left]"
    except Exception:
        pass
    return ""


def do_set(minutes):
    with locked():
        s = load()
        s.update(deadline_ts=time.time() + minutes * 60, asked_for=None, question=None, answer=None,
                 final_synced_for=None, acted_for=None, action=None)
        SAVE_NOW.unlink(missing_ok=True)
        save(s)
    log(f"deadline set to {minutes:g} min from now")
    return summarize(load())


def do_extend(minutes):
    with locked():
        s = load()
        dl = s["deadline_ts"]
        if dl is None:
            return "No deadline armed."
        if s["acted_for"] == dl:
            return "The deadline already executed; nothing to extend."
        s["deadline_ts"] = max(dl, time.time()) + minutes * 60
        s["extensions"].append({"at": time.time(), "minutes": minutes})
        s["question"], s["answer"] = None, f"user extended by {minutes:g} min"
        if s["deadline_ts"] - time.time() <= s["config"]["ask_lead_min"] * 60:
            s["asked_for"] = s["deadline_ts"]  # user just decided: don't re-ask right away
        SAVE_NOW.unlink(missing_ok=True)
        save(s)
        he, new = s["hard_end_ts"], s["deadline_ts"]
    log(f"extended by {minutes:g} min")
    warn = (f"\nWARNING: new deadline {_hhmm(new)} is past the rental hard end {_hhmm(he)}."
            if he and new > he else "")
    return summarize(load()) + warn


def do_keep():
    with locked():
        s = load()
        s["question"], s["answer"] = None, "user chose to keep the deadline as is"
        save(s)
    log("question answered: keep deadline")
    return summarize(load())


def do_end_now():
    with locked():
        s = load()
        s["deadline_ts"] = time.time()
        s["question"], s["answer"] = None, "user chose to end now"
        save(s)
    log("end-now requested")
    return "Ending now: final sync, then the deadline action. Poll time_left for the result."


def do_policy(policy):
    if policy not in ("stop", "destroy", "none"):
        return "policy must be stop | destroy | none"
    with locked():
        s = load()
        s["config"]["on_deadline"] = policy
        save(s)
    log(f"policy set to {policy}")
    return summarize(load())


def do_request_sync():
    with locked():
        s = load()
        s["sync_requested"] = True
        save(s)
    return "Sync requested; the daemon runs it within ~5s (checkpoint request + push). Poll time_left."


# ───────────────────────── MCP tools ─────────────────────────
def register(mcp):
    @mcp.tool()
    async def time_left() -> str:
        """Deadline status: minutes left, pending question, hourly rate and cost of extending,
        credit (if configured), last final sync, and whether vast.ai control works."""
        return await asyncio.to_thread(lambda: summarize(load(), with_credit=True))

    @mcp.tool()
    async def deadline_set(minutes: float) -> str:
        """Arm (or reset) the deadline `minutes` from now."""
        return do_set(minutes)

    @mcp.tool()
    async def deadline_extend(minutes: float) -> str:
        """Extend the deadline by `minutes`. Only after the user agreed (tell them the cost first)."""
        return do_extend(minutes)

    @mcp.tool()
    async def deadline_keep() -> str:
        """The user answered the deadline question: keep the current deadline."""
        return do_keep()

    @mcp.tool()
    async def deadline_end_now() -> str:
        """Final sync and deadline action right now. Only on the user's explicit instruction."""
        return do_end_now()

    @mcp.tool()
    async def deadline_set_policy(policy: str) -> str:
        """What happens at the deadline: destroy (default) | stop | none. 'destroy' deletes the
        instance and its disk (checkpoints are pushed to the Hub first; if that push fails it
        stops instead). Use 'stop' to keep the disk, or 'none' to leave it running. Change it
        only when the user asks."""
        return do_policy(policy)

    @mcp.tool()
    async def request_final_sync() -> str:
        """Ask the daemon to checkpoint (SAVE_NOW) and push now, without ending anything."""
        return do_request_sync()


# ───────────────────────── CLI ─────────────────────────
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    arg = float(sys.argv[2]) if len(sys.argv) > 2 else None
    if cmd == "daemon":
        daemon()
    elif cmd == "status":
        print(summarize(load(), with_credit=True))
    elif cmd == "selftest":
        i = instance_info()
        print(i)
        sys.exit(0 if i["ok"] else 1)
    elif cmd == "set" and arg is not None:
        print(do_set(arg))
    elif cmd == "extend" and arg is not None:
        print(do_extend(arg))
    elif cmd == "end-now":
        print(do_end_now())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
