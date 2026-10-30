"""
Remote shell MCP server for a GPU box (vast.ai / Colab).
Jobs run detached (survive disconnects). Waiting tools are async and send
keep-alive notifications every KEEPALIVE seconds so proxies/tunnels don't
drop the connection as idle.
"""
import asyncio, os, pathlib, secrets, signal, subprocess, time, uuid

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from guide import FULL_GUIDE, INSTRUCTIONS

try:
    from deadline import banner as _banner, register as _register_deadline
except Exception:  # deadline.py missing: server still works without it
    _banner, _register_deadline = (lambda: ""), None

SECRET = os.environ.get("MCP_SECRET") or secrets.token_urlsafe(24)
PORT = int(os.environ.get("PORT", "8765"))
JOBS = pathlib.Path(os.environ.get("JOBS_DIR", "/workspace/.jobs"))
JOBS.mkdir(parents=True, exist_ok=True)
MAX_WAIT = int(os.environ.get("MAX_WAIT", "55"))
KEEPALIVE = int(os.environ.get("KEEPALIVE", "10"))

mcp = FastMCP(
    "vast-gpu",
    instructions=INSTRUCTIONS,
    host="127.0.0.1",
    port=PORT,
    stateless_http=True,
    streamable_http_path=f"/{SECRET}/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _read(p: pathlib.Path) -> str:
    try:
        return p.read_text(errors="replace")
    except FileNotFoundError:
        return ""


def _result(jid: str, tail: int) -> str:
    log, ex = JOBS / f"{jid}.log", JOBS / f"{jid}.exit"
    if not (JOBS / f"{jid}.pid").exists():
        return f"unknown job {jid}"
    out = _read(log)
    if len(out) > tail:
        out = f"[... {len(out) - tail} earlier chars omitted ...]\n" + out[-tail:]
    code = _read(ex).strip()
    status = f"FINISHED exit_code={code}" if code else "RUNNING"
    return f"job {jid}: {status}\n--- output ---\n{out}{_banner()}"


def _start(cmd: str, cwd: str) -> str:
    pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
    jid = uuid.uuid4().hex[:8]
    log, ex = JOBS / f"{jid}.log", JOBS / f"{jid}.exit"
    wrapped = f'( {cmd}\n) > "{log}" 2>&1; echo $? > "{ex}"'
    p = subprocess.Popen(
        ["bash", "-c", wrapped],
        cwd=cwd,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (JOBS / f"{jid}.pid").write_text(str(p.pid))
    return jid


async def _wait(jid: str, timeout: int, tail: int, ctx: Context | None = None) -> str:
    timeout = max(1, min(timeout, MAX_WAIT))
    start = time.time()
    end = start + timeout
    last_ping = start
    ex = JOBS / f"{jid}.exit"
    while time.time() < end and not ex.exists():
        await asyncio.sleep(0.5)
        now = time.time()
        if ctx is not None and now - last_ping >= KEEPALIVE:
            last_ping = now
            elapsed = int(now - start)
            try:
                # progress notification (only delivered if client sent a progressToken)
                await ctx.report_progress(elapsed, timeout, f"job {jid} running {elapsed}s")
                # log notification (always sent on the request stream)
                await ctx.info(f"job {jid} still running ({elapsed}s)")
            except Exception:
                pass
    return _result(jid, tail)


@mcp.tool()
async def usage_guide() -> str:
    """Full guide for using this GPU box: commands and waiting, what persists, checkpoints,
    the deadline flow, security rules, troubleshooting. Read it once at the start of a session."""
    return FULL_GUIDE


@mcp.tool()
async def start_job(command: str, cwd: str = "/workspace") -> str:
    """Start a shell command in the background (installs, training, downloads).
    Returns a job_id immediately. Then call wait_job(job_id)."""
    jid = _start(command, cwd)
    return f"started job {jid}. Call wait_job('{jid}')."


@mcp.tool()
async def wait_job(job_id: str, ctx: Context, timeout: int = 45, tail_chars: int = 4000) -> str:
    """Block until the job finishes or `timeout` seconds pass (use <= 180; the client cuts a call at
    ~230 s; capped by the server's MAX_WAIT). Returns status, exit code and the last `tail_chars` of
    output. If RUNNING, just call wait_job again."""
    return await _wait(job_id, timeout, tail_chars, ctx)


@mcp.tool()
async def job_output(job_id: str, tail_chars: int = 4000) -> str:
    """Non-blocking: current status and recent output of a job."""
    return _result(job_id, tail_chars)


@mcp.tool()
async def list_jobs() -> str:
    """List jobs with their state."""
    rows = []
    for pid in sorted(JOBS.glob("*.pid"), key=lambda p: p.stat().st_mtime):
        jid = pid.stem
        code = _read(JOBS / f"{jid}.exit").strip()
        rows.append(f"{jid}  {'exit=' + code if code else 'RUNNING'}")
    return "\n".join(rows) or "no jobs"


@mcp.tool()
async def kill_job(job_id: str) -> str:
    """Kill a running job (whole process group)."""
    pidf = JOBS / f"{job_id}.pid"
    if not pidf.exists():
        return "unknown job"
    try:
        os.killpg(int(pidf.read_text()), signal.SIGKILL)
    except ProcessLookupError:
        pass
    ex = JOBS / f"{job_id}.exit"
    if not ex.exists():
        ex.write_text("137")
    return f"killed {job_id}"


@mcp.tool()
async def run_command(command: str, ctx: Context, cwd: str = "/workspace", timeout: int = 40) -> str:
    """Convenience for quick commands: start + wait (timeout <= 180). If it doesn't finish in
    `timeout` seconds it keeps running; continue with wait_job(job_id). Each call is a fresh shell."""
    jid = _start(command, cwd)
    return await _wait(jid, timeout, 4000, ctx)


@mcp.tool()
async def gpu_status() -> str:
    """nvidia-smi plus disk usage of /workspace."""
    jid = _start("nvidia-smi; echo; df -h /workspace", "/workspace")
    return await _wait(jid, 15, 4000, None)


@mcp.tool()
async def read_file(path: str, max_bytes: int = 20000) -> str:
    """Read a text file (truncated to max_bytes)."""
    data = pathlib.Path(path).read_bytes()[:max_bytes]
    return data.decode(errors="replace")


@mcp.tool()
async def write_file(path: str, content: str, append: bool = False) -> str:
    """Write (or append) text to a file, creating parent dirs."""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a" if append else "w") as f:
        f.write(content)
    return f"wrote {len(content)} chars to {path}"


from github_tools import register as _register_github  # noqa: E402

_register_github(mcp)
if _register_deadline:
    _register_deadline(mcp)

if __name__ == "__main__":
    print(f"MCP path: /{SECRET}/mcp  (port {PORT})", flush=True)
    mcp.run(transport="streamable-http")
