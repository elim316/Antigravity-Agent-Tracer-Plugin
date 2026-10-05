from collections import OrderedDict
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

PORT = int(os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT") or os.environ.get("PORT") or "8080")
HOST_TOKEN = os.environ.get("ANTIGRAVITY_SIDECAR_UI_TOKEN") or os.environ.get("ANTIGRAVITY_SIDECAR_TOKEN") or ""
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TRACER_DIR = os.path.dirname(os.path.abspath(__file__))
_SELF_FILE = os.path.abspath(__file__)
_STARTUP_MTIME = os.path.getmtime(_SELF_FILE) if os.path.exists(_SELF_FILE) else 0.0
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SUBAGENT_UUID_RE = re.compile(
    r'(?:conversation[\s_]*id"?\s*[:=]\s*"?|conversation://|brain/)'
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)
_CREATED_AT_RE = re.compile(r"^\s*Created At:\s*(\S+)", re.MULTILINE)
_COMPLETED_AT_RE = re.compile(r"^\s*Completed At:\s*(\S+)", re.MULTILINE)
_EXIT_CODE_RE = re.compile(r"command exited with code\s*(-?\d+)", re.IGNORECASE)
_FATAL_PREFIX_RE = re.compile(
    r"^\s*(Error|Encountered error|Failed|Tool execution was canceled)\b",
    re.IGNORECASE,
)
_USER_REQ_RE = re.compile(r"<USER_REQUEST>([\s\S]*?)</USER_REQUEST>")
_DURATION_SEC_RE = re.compile(r"^([0-9.]+)s$")

# ---------------------------------------------------------------------------
# Thread-safe bounded caches
# ---------------------------------------------------------------------------
_CACHE_LOCK = threading.Lock()
_MAX_TRANSCRIPT_CACHE_ENTRIES = 32
_MAX_META_CACHE_ENTRIES = 256
_MAX_TELEMETRY_CACHE_ENTRIES = 128

_UPDATE_CACHE = {"ts": 0.0, "data": None}
_UPDATE_TTL = 600.0  # 10 minutes

# conv_id -> {"mtime_ns": int, "size": int, "steps": list[dict]}
_TRANSCRIPT_STEPS_CACHE: OrderedDict[str, dict] = OrderedDict()
# conv_id -> (mtime_ns, size, summary_dict)
_CONV_META_CACHE: OrderedDict[str, tuple] = OrderedDict()
# conv_id -> (ts, mtime_ns, telemetry_dict)
_TELEMETRY_CACHE: OrderedDict[str, tuple] = OrderedDict()
_CONV_LIST_CACHE = {"ts": 0.0, "include_id": "", "items": []}
_STATIC_FILE_CACHE: dict[str, tuple[int, bytes, bytes]] = {}

_LS_DISCOVERY_CACHE = {"ts": 0.0, "addr": None, "csrf": None}
_MODEL_LABELS_CACHE = {"ts": 0.0, "map": {}}
_AUTOMATION_PATTERNS_CACHE = {"ts": 0.0, "patterns": ()}

_FALLBACK_MODEL_LABELS = {
    "MODEL_PLACEHOLDER_M260": "Gemini Next",
    "MODEL_PLACEHOLDER_M37": "Gemini Pro",
    "MODEL_PLACEHOLDER_M256": "Gemini 3.5 Pro",
    "MODEL_PLACEHOLDER_M257": "Gemini 3.5 Pro (Low Thinking)",
    "MODEL_PLACEHOLDER_M273": "Gemini 3.5 Pro",
    "MODEL_PLACEHOLDER_M196": "Gemini 3.6 Flash",
    "MODEL_PLACEHOLDER_M198": "Gemini 3.5 Flash Lite",
    "MODEL_PLACEHOLDER_M200": "Gemini 3.5 Flash",
    "MODEL_PLACEHOLDER_M264": "Gemini 3.6 Flash (High)",
    "MODEL_PLACEHOLDER_M265": "Gemini 3.6 Flash",
    "MODEL_PLACEHOLDER_M298": "Gemini 3.7 Flash",
    "MODEL_PLACEHOLDER_M65": "Claude Opus 4.6 (Thinking)",
    "MODEL_PLACEHOLDER_M64": "Claude Sonnet 4.6 (Thinking)",
    "MODEL_GOOGLE_GEMINI_2_5_PRO": "Gemini 2.5 Pro",
    "MODEL_GOOGLE_GEMINI_2_5_FLASH": "Gemini 2.5 Flash",
    "MODEL_GOOGLE_GEMINI_2_5_FLASH_LITE": "Gemini 2.5 Flash Lite",
    "MODEL_GOOGLE_GEMINI_INTERNAL_BYOM": "Gemini Internal",
    "MODEL_CLAUDE_4_SONNET": "Claude Sonnet 4",
    "MODEL_CLAUDE_4_OPUS": "Claude Opus 4",
}

# Pricing per 1M tokens: (uncached_input, cached_input, output_and_thinking)
_TIER_PRICING_PER_M = {
    "lite": (0.10, 0.025, 0.40),
    "flash": (0.30, 0.075, 2.50),
    "pro": (1.25, 0.3125, 10.00),
    "sonnet": (3.00, 0.30, 15.00),
    "opus": (15.00, 1.50, 75.00),
}


def _lru_put(store: OrderedDict, key: str, value, max_entries: int):
    with _CACHE_LOCK:
        if key in store:
            store.move_to_end(key)
        store[key] = value
        while len(store) > max_entries:
            store.popitem(last=False)


def _lru_get(store: OrderedDict, key: str):
    with _CACHE_LOCK:
        val = store.get(key)
        if val is not None:
            store.move_to_end(key)
        return val


# ---------------------------------------------------------------------------
# Git update & safe fast-forward maintenance
# ---------------------------------------------------------------------------
def _git(args: list[str], timeout: int = 8) -> str:
    res = subprocess.run(
        ["git", "-C", REPO_ROOT] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"git {args[0]} exited {res.returncode}")
    return res.stdout.strip()


def _check_git_update(force: bool = False) -> dict:
    now = time.time()
    with _CACHE_LOCK:
        if not force and _UPDATE_CACHE["data"] is not None and (now - _UPDATE_CACHE["ts"]) < _UPDATE_TTL:
            return _UPDATE_CACHE["data"]

    try:
        _git(["fetch", "--quiet", "origin"], timeout=8)
        local_sha = _git(["rev-parse", "--short", "HEAD"])
        upstream_sha = _git(["rev-parse", "--short", "@{u}"])
        behind = int(_git(["rev-list", "--count", "HEAD..@{u}"]) or "0")
        ahead = int(_git(["rev-list", "--count", "@{u}..HEAD"]) or "0")
        dirty = bool(_git(["status", "--porcelain", "--untracked-files=no"]))
        commits = []
        if behind > 0:
            log_out = _git(["log", "--oneline", "-n", "5", "HEAD..@{u}"])
            commits = [line for line in log_out.splitlines() if line.strip()]
        data = {
            "supported": True,
            "local_sha": local_sha,
            "remote_sha": upstream_sha,
            "behind": behind,
            "ahead": ahead,
            "dirty": dirty,
            "update_available": behind > 0,
            "commits": commits,
            "checked_at": int(now),
        }
    except Exception as exc:
        data = {
            "supported": False,
            "update_available": False,
            "behind": 0,
            "ahead": 0,
            "error": str(exc),
            "checked_at": int(now),
        }

    with _CACHE_LOCK:
        _UPDATE_CACHE["ts"] = now
        _UPDATE_CACHE["data"] = data
    return data


def _perform_update() -> dict:
    """Safely fast-forwards to @{u} without destroying local commits or dirty edits."""
    try:
        status = _check_git_update(force=True)
        if status.get("dirty") or status.get("ahead", 0) > 0:
            return {
                "ok": False,
                "error": "Working tree has local changes or unpushed commits; skipping automatic merge.",
                "status": status,
            }
        _git(["merge", "--ff-only", "@{u}"], timeout=10)
        new_sha = _git(["rev-parse", "--short", "HEAD"])
        with _CACHE_LOCK:
            _UPDATE_CACHE["ts"] = 0.0
            _UPDATE_CACHE["data"] = None
        return {"ok": True, "new_sha": new_sha, "restarting": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _background_maintenance_loop():
    """Monitors server.py for hot-restarts every 5s and checks clean fast-forward updates every 15m."""
    last_sync = time.time()
    while True:
        try:
            if os.path.exists(_SELF_FILE) and os.path.getmtime(_SELF_FILE) != _STARTUP_MTIME:
                os._exit(0)
            now = time.time()
            if (now - last_sync) >= 900.0:
                last_sync = now
                status = _check_git_update(force=True)
                if (
                    status.get("supported")
                    and status.get("update_available")
                    and not status.get("dirty")
                    and status.get("ahead", 0) == 0
                ):
                    res = _perform_update()
                    if res.get("ok"):
                        time.sleep(1.0)
                        os._exit(0)
        except Exception:
            pass
        time.sleep(5)


# ---------------------------------------------------------------------------
# Dynamic Automation & Transcript Parsing
# ---------------------------------------------------------------------------
def _get_automation_patterns() -> tuple[str, ...]:
    """Discovers automation patterns dynamically from installed sidecars and env config."""
    now = time.time()
    if _AUTOMATION_PATTERNS_CACHE["patterns"] and (now - _AUTOMATION_PATTERNS_CACHE["ts"]) < 300.0:
        return _AUTOMATION_PATTERNS_CACHE["patterns"]

    patterns = {
        "<automation",
        "[automation",
        "scheduled automation",
        "cron schedule",
        "step 0 - window",
    }
    env_extra = os.environ.get("AGENT_TRACER_AUTOMATION_PATTERNS", "")
    for item in env_extra.split(","):
        cleaned = item.strip().lower()
        if cleaned:
            patterns.add(cleaned)

    # Inspect installed plugin sidecar manifests for non-UI daemon/cron sidecars
    plugins_root = os.path.expanduser("~/.gemini/config/plugins")
    if os.path.isdir(plugins_root):
        try:
            for p_name in os.listdir(plugins_root):
                sc_root = os.path.join(plugins_root, p_name, "sidecars")
                if not os.path.isdir(sc_root):
                    continue
                for s_name in os.listdir(sc_root):
                    manifest_path = os.path.join(sc_root, s_name, "sidecar.json")
                    if not os.path.isfile(manifest_path):
                        continue
                    try:
                        with open(manifest_path, "r", encoding="utf-8") as mf:
                            meta = json.load(mf)
                        if not meta.get(" panels") and not meta.get("panels"):
                            disp = (meta.get("display_name") or "").strip().lower()
                            if disp and len(disp) >= 4:
                                patterns.add(disp)
                    except Exception:
                        continue
        except OSError:
            pass

    compiled = tuple(sorted(patterns))
    _AUTOMATION_PATTERNS_CACHE["ts"] = now
    _AUTOMATION_PATTERNS_CACHE["patterns"] = compiled
    return compiled


def _clean_user_prompt(raw: str) -> str:
    if not raw:
        return ""
    m = _USER_REQ_RE.search(raw)
    text = m.group(1) if m else raw
    text = re.sub(r"<[A-Z_]+>[\s\S]*?</[A-Z_]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _load_cached_transcript_steps(t_path: str, conv_id: str) -> list[dict]:
    """Loads transcript steps with byte-offset incremental tailing for O(1) polling."""
    try:
        st = os.stat(t_path)
    except OSError:
        return []

    mtime_ns = st.st_mtime_ns
    file_size = st.st_size
    cached = _lru_get(_TRANSCRIPT_STEPS_CACHE, conv_id)

    if cached is not None:
        if cached["mtime_ns"] == mtime_ns and cached["size"] == file_size:
            return cached["steps"]

        # Append-only fast path: read only newly appended bytes
        if file_size > cached["size"] and mtime_ns >= cached["mtime_ns"]:
            try:
                new_steps = list(cached["steps"])
                by_idx = {s.get("step_index"): i for i, s in enumerate(new_steps)}
                with open(t_path, "rb") as bf:
                    bf.seek(cached["size"])
                    tail_bytes = bf.read()
                for raw_line in tail_bytes.decode("utf-8", errors="replace").splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        step = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sidx = step.get("step_index")
                    if sidx is not None and sidx in by_idx:
                        new_steps[by_idx[sidx]] = step
                    else:
                        if sidx is not None:
                            by_idx[sidx] = len(new_steps)
                        new_steps.append(step)
                entry = {"mtime_ns": mtime_ns, "size": file_size, "steps": new_steps}
                _lru_put(_TRANSCRIPT_STEPS_CACHE, conv_id, entry, _MAX_TRANSCRIPT_CACHE_ENTRIES)
                return new_steps
            except OSError:
                pass

    # Full parse path (initial load or file truncated/rewritten)
    steps: list[dict] = []
    by_idx: dict[int, int] = {}
    try:
        with open(t_path, "r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    step = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sidx = step.get("step_index")
                if sidx is not None and sidx in by_idx:
                    steps[by_idx[sidx]] = step
                else:
                    if sidx is not None:
                        by_idx[sidx] = len(steps)
                    steps.append(step)
    except OSError:
        return []

    entry = {"mtime_ns": mtime_ns, "size": file_size, "steps": steps}
    _lru_put(_TRANSCRIPT_STEPS_CACHE, conv_id, entry, _MAX_TRANSCRIPT_CACHE_ENTRIES)
    return steps


def _summarize_conversation(conv_id: str, t_path: str) -> dict | None:
    try:
        st = os.stat(t_path)
    except OSError:
        return None

    mtime_ns = st.st_mtime_ns
    file_size = st.st_size
    cached = _lru_get(_CONV_META_CACHE, conv_id)
    if cached and cached[0] == mtime_ns and cached[1] == file_size:
        return cached[2]

    first_prompt = ""
    first_ts = ""
    last_ts = ""
    turn_count = 0
    step_count = 0
    tool_count = 0
    error_count = 0
    last_tool = ""
    last_status = "DONE"
    last_type = ""
    subagents = []
    seen_sub_ids = set()

    steps = _load_cached_transcript_steps(t_path, conv_id)
    for step in steps:
        step_count += 1
        c_at = step.get("created_at") or ""
        if c_at:
            if not first_ts:
                first_ts = c_at
            last_ts = c_at

        stype = step.get("type") or ""
        last_type = stype
        last_status = step.get("status") or "DONE"
        if stype == "USER_INPUT":
            turn_count += 1
            if not first_prompt:
                first_prompt = _clean_user_prompt(step.get("content", ""))
        elif stype == "ERROR_MESSAGE":
            error_count += 1
        elif stype == "PLANNER_RESPONSE":
            tcalls = step.get("tool_calls") or []
            tool_count += len(tcalls)
            for tc in tcalls:
                tname = tc.get("name") or ""
                if tname:
                    last_tool = tname
                if tname == "invoke_subagent":
                    targs = tc.get("arguments") or tc.get("args") or {}
                    if isinstance(targs, str):
                        try:
                            targs = json.loads(targs)
                        except Exception:
                            targs = {}
                    sub_specs = targs.get("Subagents") if isinstance(targs, dict) else []
                    if isinstance(sub_specs, str):
                        try:
                            sub_specs = json.loads(sub_specs)
                        except Exception:
                            sub_specs = []
                    if isinstance(sub_specs, list) and sub_specs:
                        for sp in sub_specs:
                            if isinstance(sp, dict):
                                role = str(sp.get("Role") or sp.get("TypeName") or "Subagent").strip('"')
                                subagents.append({
                                    "role": role,
                                    "typeName": str(sp.get("TypeName") or "subagent").strip('"'),
                                    "prompt": str(sp.get("Prompt") or "").strip('"')[:160],
                                    "id": "",
                                })
                    elif isinstance(targs, dict):
                        role = str(targs.get("Role") or targs.get("TypeName") or "Subagent").strip('"')
                        subagents.append({
                            "role": role,
                            "typeName": str(targs.get("TypeName") or "subagent").strip('"'),
                            "prompt": str(targs.get("Prompt") or "").strip('"')[:160],
                            "id": "",
                        })
        elif stype == "GENERIC":
            content = step.get("content") or ""
            body = _COMPLETED_AT_RE.sub("", _CREATED_AT_RE.sub("", content)).strip()
            if _FATAL_PREFIX_RE.search(body):
                error_count += 1
            else:
                head = body.split("\nOutput:")[0]
                m_exit = _EXIT_CODE_RE.search(head)
                if m_exit and m_exit.group(1) != "0":
                    error_count += 1
            low_c = content.lower()
            if "conversation" in low_c or "subagent" in low_c or "brain/" in low_c:
                for m in _SUBAGENT_UUID_RE.finditer(content):
                    sid = m.group(1)
                    if sid != conv_id and sid not in seen_sub_ids:
                        seen_sub_ids.add(sid)
                        for s_item in subagents:
                            if not s_item["id"]:
                                s_item["id"] = sid
                                break
                        else:
                            subagents.append({"role": "Subagent", "typeName": "subagent", "prompt": "", "id": sid})

    p_lower = first_prompt.lower()
    auto_patterns = _get_automation_patterns()
    is_automation = any(pat in p_lower for pat in auto_patterns)
    title = first_prompt[:90] + ("…" if len(first_prompt) > 90 else "") if first_prompt else f"Session {conv_id[:8]}"

    duration_ms = 0
    if first_ts and last_ts:
        try:
            from datetime import datetime
            t0 = datetime.fromisoformat(first_ts.replace("Z", "+00:00")).timestamp()
            t1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00")).timestamp()
            if t1 >= t0:
                duration_ms = int((t1 - t0) * 1000)
        except Exception:
            duration_ms = 0

    age_s = time.time() - st.st_mtime
    if last_status in ("RUNNING", "IN_PROGRESS", "PENDING") or (age_s < 18 and last_type in ("USER_INPUT", "PLANNER_RESPONSE")):
        status_str = "RUNNING"
    elif last_status == "ERROR" or last_type == "ERROR_MESSAGE":
        status_str = "ERROR"
    else:
        status_str = "IDLE"

    meta = {
        "id": conv_id,
        "title": title,
        "turns": turn_count,
        "steps": step_count,
        "stepCount": step_count,
        "tools": tool_count,
        "toolCount": tool_count,
        "errors": error_count,
        "status": status_str,
        "lastTool": last_tool,
        "durationMs": duration_ms,
        "createdAt": first_ts,
        "updatedAt": last_ts,
        "mtime": st.st_mtime,
        "isAutomation": is_automation,
        "isSubagent": False,
        "parentId": None,
        "subagents": [s for s in subagents if s.get("id")],
    }
    _lru_put(_CONV_META_CACHE, conv_id, (mtime_ns, file_size, meta), _MAX_META_CACHE_ENTRIES)
    return meta


def _summarize_subagent_live(conv_id: str, t_path: str) -> dict:
    """Computes live status for a child subagent conversation using cached steps."""
    steps = _load_cached_transcript_steps(t_path, conv_id)
    if not steps:
        return {"id": conv_id, "available": False}

    step_count = len(steps)
    tool_count = 0
    error_count = 0
    last_status = "DONE"
    last_type = ""
    last_tool_name = ""
    first_ts = ""
    last_ts = ""

    for step in steps:
        c_at = step.get("created_at") or ""
        if c_at:
            if not first_ts:
                first_ts = c_at
            last_ts = c_at

        last_status = step.get("status") or "DONE"
        last_type = step.get("type") or ""

        if last_type == "ERROR_MESSAGE":
            error_count += 1
        elif last_type == "PLANNER_RESPONSE":
            tcalls = step.get("tool_calls") or []
            tool_count += len(tcalls)
            if tcalls:
                last_tool_name = tcalls[-1].get("name") or last_tool_name
        elif last_type == "GENERIC":
            raw = step.get("content") or ""
            body = _COMPLETED_AT_RE.sub("", _CREATED_AT_RE.sub("", raw)).strip()
            if _FATAL_PREFIX_RE.search(body):
                error_count += 1
            else:
                head = body.split("\nOutput:")[0]
                m = _EXIT_CODE_RE.search(head)
                if m and m.group(1) != "0":
                    error_count += 1

    try:
        mtime = os.path.getmtime(t_path)
    except OSError:
        mtime = 0.0
    age_s = max(0.0, time.time() - mtime) if mtime else 999999.0
    is_running = (
        last_status not in ("DONE", "ERROR", "CANCELED", "CANCELLED", "FAILED")
        or (age_s < 12.0 and last_type != "PLANNER_RESPONSE")
    )
    status_label = "RUNNING" if is_running else ("ERROR" if last_status == "ERROR" else "DONE")

    duration_ms = 0
    if first_ts and last_ts:
        try:
            from datetime import datetime
            t0 = datetime.fromisoformat(first_ts.replace("Z", "+00:00")).timestamp()
            t1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00")).timestamp()
            if t1 >= t0:
                duration_ms = int((t1 - t0) * 1000)
        except Exception:
            pass

    return {
        "id": conv_id,
        "available": True,
        "status": status_label,
        "steps": step_count,
        "tools": tool_count,
        "errors": error_count,
        "lastTool": last_tool_name,
        "durationMs": duration_ms,
        "updatedAt": last_ts,
    }


# ---------------------------------------------------------------------------
# Language Server Connect-RPC Discovery & Telemetry
# ---------------------------------------------------------------------------
def _call_ls_rpc(addr: str, csrf: str, method: str, payload: dict, timeout: float = 2.0) -> dict | None:
    url = f"http://{addr}/exa.language_server_pb.LanguageServerService/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-codeium-csrf-token": csrf,
        },
        method="POST",
    )
    try:
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _discover_language_server() -> tuple[str | None, str | None]:
    """Discovers and verifies the local Jetski Language Server Connect-RPC endpoint."""
    now = time.time()
    if _LS_DISCOVERY_CACHE["addr"] and (now - _LS_DISCOVERY_CACHE["ts"]) < 60.0:
        return _LS_DISCOVERY_CACHE["addr"], _LS_DISCOVERY_CACHE["csrf"]

    csrf = None
    target_pid = None
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    raw = f.read().decode("utf-8", errors="ignore")
                if "language_server" in raw and "--csrf_token" in raw:
                    args = raw.split("\x00")
                    for i, arg in enumerate(args):
                        if arg == "--csrf_token" and i + 1 < len(args):
                            csrf = args[i + 1]
                            target_pid = pid
                            break
                        if arg.startswith("--csrf_token="):
                            csrf = arg.split("=", 1)[1]
                            target_pid = pid
                            break
                if csrf and target_pid:
                    break
            except OSError:
                continue
    except OSError:
        pass

    if not csrf or not target_pid:
        return None, None

    try:
        out = subprocess.check_output(["ss", "-tlpn"], text=True, stderr=subprocess.DEVNULL, timeout=2)
        ports = []
        needle = f"pid={target_pid},"
        for line in out.splitlines():
            if needle in line:
                m = re.search(r"127\.0\.0\.1:(\d+)", line)
                if m:
                    ports.append(int(m.group(1)))
        for p in sorted(set(ports)):
            candidate_addr = f"127.0.0.1:{p}"
            # Probe candidate port to confirm it speaks Connect-RPC HTTP
            probe = _call_ls_rpc(candidate_addr, csrf, "GetCascadeModelConfigData", {}, timeout=0.8)
            if probe is not None:
                _LS_DISCOVERY_CACHE.update({"ts": now, "addr": candidate_addr, "csrf": csrf})
                return candidate_addr, csrf
        if ports:
            fallback_addr = f"127.0.0.1:{min(ports)}"
            _LS_DISCOVERY_CACHE.update({"ts": now, "addr": fallback_addr, "csrf": csrf})
            return fallback_addr, csrf
    except Exception:
        pass
    return None, None


def _get_model_display_name(raw_model: str) -> str:
    """Resolves internal MODEL_PLACEHOLDER_* enums via Language Server RPC + fallback map."""
    if not raw_model:
        return "Gemini Next"
    now = time.time()
    if not _MODEL_LABELS_CACHE["map"] or (now - _MODEL_LABELS_CACHE["ts"]) > 300.0:
        merged = dict(_FALLBACK_MODEL_LABELS)
        addr, csrf = _discover_language_server()
        if addr and csrf:
            avail = _call_ls_rpc(addr, csrf, "GetAvailableModels", {}, timeout=1.5)
            if isinstance(avail, dict):
                models = (avail.get("response") or {}).get("models") or {}
                for info in models.values():
                    if isinstance(info, dict) and info.get("model") and info.get("displayName"):
                        merged[info["model"]] = info["displayName"]
            cfg_data = _call_ls_rpc(addr, csrf, "GetCascadeModelConfigData", {}, timeout=1.5)
            if isinstance(cfg_data, dict):
                for cfg in cfg_data.get("clientModelConfigs") or []:
                    if isinstance(cfg, dict):
                        m_enum = (cfg.get("modelOrAlias") or {}).get("model")
                        lbl = cfg.get("label")
                        if m_enum and lbl:
                            merged[m_enum] = lbl
        _MODEL_LABELS_CACHE["map"] = merged
        _MODEL_LABELS_CACHE["ts"] = now

    label = _MODEL_LABELS_CACHE["map"].get(raw_model)
    if label:
        return label
    if raw_model.startswith("MODEL_PLACEHOLDER_"):
        return "Gemini Next"
    return raw_model.replace("MODEL_GOOGLE_", "").replace("MODEL_", "").replace("_", " ").title()


def _estimate_cost_usd(raw_model: str, display_label: str, uncached_in: int, cached_in: int, out_plus_think: int) -> float:
    combined = f"{raw_model} {display_label}".lower()
    if "opus" in combined:
        tier = "opus"
    elif "sonnet" in combined or "claude" in combined:
        tier = "sonnet"
    elif "lite" in combined:
        tier = "lite"
    elif "flash" in combined:
        tier = "flash"
    else:
        tier = "pro"
    in_rate, cache_rate, out_rate = _TIER_PRICING_PER_M[tier]
    return (uncached_in * in_rate + cached_in * cache_rate + out_plus_think * out_rate) / 1_000_000.0


def _get_token_telemetry(conv_id: str, t_path: str | None = None) -> dict:
    if not conv_id or not _UUID_RE.match(conv_id):
        return {"available": False}

    now = time.time()
    mtime_ns = 0
    if t_path and os.path.isfile(t_path):
        try:
            mtime_ns = os.stat(t_path).st_mtime_ns
        except OSError:
            mtime_ns = 0

    cached = _lru_get(_TELEMETRY_CACHE, conv_id)
    if cached:
        c_ts, c_mtime_ns, c_data = cached
        # If transcript file hasn't changed, cache stays valid up to 60s; otherwise 12s
        ttl = 60.0 if (mtime_ns and c_mtime_ns == mtime_ns) else 12.0
        if (now - c_ts) < ttl:
            return c_data

    addr, csrf = _discover_language_server()
    if not addr or not csrf:
        return {"available": False}

    data = _call_ls_rpc(addr, csrf, "GetCascadeTrajectory", {"cascade_id": conv_id}, timeout=3.0)
    if not data or not isinstance(data.get("trajectory"), dict):
        # Clear discovery cache on RPC failure so next attempt re-probes
        _LS_DISCOVERY_CACHE["ts"] = 0.0
        return {"available": False}

    gen_meta = data["trajectory"].get("generatorMetadata") or []
    if not gen_meta:
        res = {"available": False}
        _lru_put(_TELEMETRY_CACHE, conv_id, (now, mtime_ns, res), _MAX_TELEMETRY_CACHE_ENTRIES)
        return res

    total_input = 0
    total_output = 0
    total_thinking = 0
    total_cache_read = 0
    total_ttft_ms = 0
    total_stream_ms = 0
    model_enum = ""
    provider = ""
    steps_telemetry = []

    for g in gen_meta:
        cm = g.get("chatModel") or {}
        u = cm.get("usage") or {}
        m = u.get("model") or cm.get("model") or ""
        if m:
            model_enum = m
        prov = u.get("apiProvider") or ""
        if prov:
            provider = prov

        inp = int(u.get("inputTokens") or 0)
        out = int(u.get("outputTokens") or 0)
        thk = int(u.get("thinkingOutputTokens") or 0)
        crd = int(u.get("cacheReadTokens") or 0)

        total_input += inp
        total_output += out
        total_thinking += thk
        total_cache_read += crd

        ttft_s = str(cm.get("timeToFirstToken") or "0s")
        stream_s = str(cm.get("streamingDuration") or "0s")
        m_ttft = _DURATION_SEC_RE.match(ttft_s)
        m_strm = _DURATION_SEC_RE.match(stream_s)
        ttft_ms = int(float(m_ttft.group(1)) * 1000) if m_ttft else 0
        strm_ms = int(float(m_strm.group(1)) * 1000) if m_strm else 0
        total_ttft_ms += ttft_ms
        total_stream_ms += strm_ms

        steps_telemetry.append({
            "stepIndices": g.get("stepIndices") or [],
            "inputTokens": inp,
            "outputTokens": out,
            "thinkingTokens": thk,
            "cacheReadTokens": crd,
            "ttftMs": ttft_ms,
            "streamMs": strm_ms,
        })

    model_label = _get_model_display_name(model_enum)
    est_cost = _estimate_cost_usd(
        model_enum,
        model_label,
        total_input,
        total_cache_read,
        total_output + total_thinking,
    )
    prompt_total = total_input + total_cache_read
    cache_hit_pct = round((total_cache_read / prompt_total) * 100, 1) if prompt_total > 0 else 0.0

    res = {
        "available": True,
        "model": model_label,
        "modelRaw": model_enum,
        "modelEnum": model_enum,
        "provider": provider.replace("API_PROVIDER_", ""),
        "generations": len(gen_meta),
        "inputTokens": total_input,
        "uncachedInputTokens": total_input,
        "cacheReadTokens": total_cache_read,
        "promptTokens": prompt_total,
        "outputTokens": total_output,
        "thinkingTokens": total_thinking,
        "totalTokens": prompt_total + total_output + total_thinking,
        "cacheHitPct": cache_hit_pct,
        "totalTtftMs": total_ttft_ms,
        "totalStreamMs": total_stream_ms,
        "estimatedCostUsd": round(est_cost, 4),
        "steps": steps_telemetry,
    }
    _lru_put(_TELEMETRY_CACHE, conv_id, (now, mtime_ns, res), _MAX_TELEMETRY_CACHE_ENTRIES)
    return res


def _get_static_asset(file_path: str) -> tuple[bytes, bytes] | None:
    """Returns (raw_bytes, gzip_bytes) for a static asset, cached in memory by mtime_ns."""
    try:
        st = os.stat(file_path)
    except OSError:
        return None
    mtime_ns = st.st_mtime_ns
    with _CACHE_LOCK:
        cached = _STATIC_FILE_CACHE.get(file_path)
        if cached and cached[0] == mtime_ns:
            return cached[1], cached[2]

    try:
        with open(file_path, "rb") as f:
            raw_bytes = f.read()
        gzip_bytes = gzip.compress(raw_bytes, compresslevel=6)
    except OSError:
        return None

    with _CACHE_LOCK:
        _STATIC_FILE_CACHE[file_path] = (mtime_ns, raw_bytes, gzip_bytes)
    return raw_bytes, gzip_bytes


def _extract_conv_id(qs: dict[str, list[str]]) -> str:
    """Extracts a conversation ID from query parameters using any standard key."""
    return (
        qs.get("conversationId", [""])[0]
        or qs.get("convId", [""])[0]
        or qs.get("activeId", [""])[0]
        or ""
    ).strip()


# ---------------------------------------------------------------------------
# HTTP Request Handler
# ---------------------------------------------------------------------------
class AgentTracerHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    @staticmethod
    def _brain_roots() -> list[str]:
        env_dir = os.environ.get("AGENT_TRACER_BRAIN_DIR")
        return [
            *([os.path.expanduser(env_dir)] if env_dir else []),
            os.path.expanduser("~/.gemini/jetski/brain"),
            os.path.expanduser("~/.gemini/antigravity/brain"),
        ]

    def _transcript_path(self, conv_id: str, full: bool = False) -> str | None:
        if not conv_id or not _UUID_RE.match(conv_id):
            return None
        name = "transcript_full.jsonl" if full else "transcript.jsonl"
        candidates = self._brain_roots()
        for root in candidates:
            p = os.path.join(root, conv_id, ".system_generated/logs", name)
            if os.path.exists(p):
                return p
        return os.path.join(candidates[0], conv_id, ".system_generated/logs", name)

    def _list_conversations(self, include_id: str = "") -> list[dict]:
        now = time.time()
        with _CACHE_LOCK:
            if (
                _CONV_LIST_CACHE["items"]
                and _CONV_LIST_CACHE["include_id"] == include_id
                and (now - _CONV_LIST_CACHE["ts"]) < 10.0
            ):
                return _CONV_LIST_CACHE["items"]

        seen = {}
        for root in self._brain_roots():
            if not os.path.isdir(root):
                continue
            try:
                entries = os.listdir(root)
            except OSError:
                continue
            for cid in entries:
                if not _UUID_RE.match(cid) or cid in seen:
                    continue
                t_path = os.path.join(root, cid, ".system_generated/logs/transcript.jsonl")
                if os.path.isfile(t_path):
                    try:
                        seen[cid] = (t_path, os.path.getmtime(t_path))
                    except OSError:
                        pass

        # Pre-sort by mtime descending and only inspect the top 65 + requested conversation
        ranked_cids = sorted(seen.keys(), key=lambda c: seen[c][1], reverse=True)
        selected_cids = ranked_cids[:65]
        if include_id and include_id in seen and include_id not in selected_cids:
            selected_cids.append(include_id)

        items = []
        by_id = {}
        for cid in selected_cids:
            t_path, _ = seen[cid]
            meta = _summarize_conversation(cid, t_path)
            if meta and (meta["stepCount"] > 0 or cid == include_id):
                # Shallow copy so parent-subagent annotations don't mutate cached meta
                meta_copy = dict(meta)
                items.append(meta_copy)
                by_id[cid] = meta_copy

        for item in items:
            for sub in item.get("subagents") or []:
                sid = sub.get("id")
                if sid and sid in by_id:
                    by_id[sid]["isSubagent"] = True
                    by_id[sid]["parentId"] = item["id"]
                    if by_id[sid]["title"].startswith("Session ") and sub.get("role"):
                        by_id[sid]["title"] = sub["role"]

        items.sort(key=lambda x: x.get("mtime", 0.0), reverse=True)
        res_items = items[:55]
        with _CACHE_LOCK:
            _CONV_LIST_CACHE.update({"ts": now, "include_id": include_id, "items": res_items})
        return res_items

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200, gzip_body: bytes | None = None):
        accept_enc = self.headers.get("Accept-Encoding", "")
        use_gzip = gzip_body is not None and "gzip" in accept_enc
        payload = gzip_body if use_gzip else body

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status=status, gzip_body=None)

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)

        if parsed_path.path in ("/", "/index.html"):
            html_path = os.path.join(TRACER_DIR, "index.html")
            asset = _get_static_asset(html_path)
            if asset is None:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"Error loading index.html")
                return
            raw_bytes, _ = asset
            self._send_bytes(raw_bytes, "text/html; charset=utf-8", status=200, gzip_body=None)
            return

        elif parsed_path.path == "/api/update-status":
            qs = urllib.parse.parse_qs(parsed_path.query)
            force = qs.get("force", ["0"])[0] == "1"
            self._send_json(_check_git_update(force=force))
            return

        elif parsed_path.path == "/api/conversations":
            qs = urllib.parse.parse_qs(parsed_path.query)
            include_id = _extract_conv_id(qs)
            self._send_json({"conversations": self._list_conversations(include_id=include_id)})
            return

        elif parsed_path.path == "/api/telemetry":
            qs = urllib.parse.parse_qs(parsed_path.query)
            conv_id = _extract_conv_id(qs)
            t_path = self._transcript_path(conv_id, full=False) if conv_id else None
            self._send_json(_get_token_telemetry(conv_id, t_path=t_path))
            return

        elif parsed_path.path == "/api/subagents_status":
            qs = urllib.parse.parse_qs(parsed_path.query)
            raw_ids = qs.get("ids", [""])[0]
            ids = [x.strip() for x in raw_ids.split(",") if x.strip() and _UUID_RE.match(x.strip())][:30]
            result = {}
            for cid in ids:
                t_path = self._transcript_path(cid, full=False)
                if t_path and os.path.isfile(t_path):
                    result[cid] = _summarize_subagent_live(cid, t_path)
                else:
                    result[cid] = {"id": cid, "available": False}
            self._send_json({"subagents": result})
            return

        elif parsed_path.path == "/api/transcript":
            qs = urllib.parse.parse_qs(parsed_path.query)
            conv_id = _extract_conv_id(qs)
            try:
                since = int(qs.get("since", ["-1"])[0])
            except ValueError:
                since = -1

            t_path = self._transcript_path(conv_id, full=False)
            if not t_path or not os.path.isfile(t_path):
                self._send_json([])
                return

            all_steps = _load_cached_transcript_steps(t_path, conv_id)
            if since < 0:
                self._send_json(all_steps)
            else:
                filtered = [s for s in all_steps if s.get("step_index", -1) > since]
                self._send_json(filtered)
            return

        elif parsed_path.path == "/api/step_full":
            qs = urllib.parse.parse_qs(parsed_path.query)
            conv_id = _extract_conv_id(qs)
            try:
                target = int(qs.get("step", ["-1"])[0])
            except ValueError:
                target = -1

            found = None
            t_path = self._transcript_path(conv_id, full=True)
            if t_path and target >= 0 and os.path.isfile(t_path):
                try:
                    with open(t_path, "r", encoding="utf-8", errors="replace") as f:
                        for raw_line in f:
                            line = raw_line.strip()
                            if not line:
                                continue
                            try:
                                step = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if step.get("step_index") == target:
                                found = step
                                break
                except Exception as exc:
                    found = {"error": str(exc)}

            self._send_json(found or {})
            return

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path)

        if parsed_path.path in ("/api/update", "/api/self-update"):
            result = _perform_update()
            self._send_json(result, status=200 if result.get("ok") else 500)
            if result.get("ok"):
                def _restart():
                    time.sleep(0.4)
                    os._exit(0)
                threading.Thread(target=_restart, daemon=True).start()
        else:
            self.send_response(404)
            self.end_headers()


class ThreadedTracerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    t = threading.Thread(target=_background_maintenance_loop, daemon=True)
    t.start()

    server = ThreadedTracerServer(("0.0.0.0", PORT), AgentTracerHandler)
    actual_port = server.server_address[1]
    print(f"PORT={actual_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
