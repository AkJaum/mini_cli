import base64
import hmac
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp
from threading import Lock
from urllib.request import urlopen

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from runner_service.pty_sessions import PtySessionManager

logger = logging.getLogger("runner_service")

CODE_EXTENSIONS = {
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
    ".rb", ".php", ".swift", ".kt", ".kts", ".lua", ".sh", ".bash",
    ".zsh", ".sql", ".html", ".css", ".scss", ".json", ".yaml",
    ".yml", ".toml", ".ini", ".conf", ".xml", ".dockerfile",
}

PROJECT_SESSIONS = {}
RATE_LIMIT_STATE = {}
INFLIGHT_STATE = {}
SECURITY_LOCK = Lock()
SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{8,128}$")
PTY_MANAGER = PtySessionManager(
    max_buffer_bytes=settings.MAX_PTY_BUFFER_BYTES,
    max_lifetime_seconds=settings.PTY_MAX_LIFETIME_SECONDS,
)
WEB_SESSIONS = {}
WEB_LOCK = Lock()
WORKSPACE_LOCK = Lock()


def _safe_internal_error(message="erro interno do servidor"):
    return _json_error(message, 500)


def _truncate_output(value):
    text = value or ""
    if len(text.encode("utf-8", errors="ignore")) <= settings.MAX_OUTPUT_BYTES:
        return text

    encoded = text.encode("utf-8", errors="ignore")[-settings.MAX_OUTPUT_BYTES:]
    truncated = encoded.decode("utf-8", errors="ignore")
    return (
        "[runner] início da saída truncado por limite de segurança\n\n"
        f"{truncated}"
    )


def _combine_process_output(stdout, stderr):
    parts = [part for part in [stdout or "", stderr or ""] if part]
    if not parts:
        return ""

    return "\n".join(parts)


def _extract_client_id(request, body=None):
    header_id = request.headers.get("X-Client-Id", "").strip()
    if header_id:
        return header_id[:128]

    if isinstance(body, dict):
        payload_id = body.get("clientId")
        if isinstance(payload_id, str) and payload_id.strip():
            return payload_id.strip()[:128]

        project_id = body.get("projectId")
        if isinstance(project_id, str) and project_id.strip():
            return f"project:{project_id.strip()[:120]}"

    forwarded = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()[:100]}"

    remote_addr = request.META.get("REMOTE_ADDR", "unknown")
    return f"ip:{str(remote_addr)[:100]}"


def _is_authorized(request):
    if not settings.RUNNER_SHARED_TOKEN:
        return True

    token = request.headers.get("X-Runner-Token", "")
    return hmac.compare_digest(token.encode(), settings.RUNNER_SHARED_TOKEN.encode())


def _prune_stale_locked(now_monotonic):
    stale_cutoff = now_monotonic - settings.PENDING_REQUEST_TTL_SECONDS
    stale_clients = []
    for client_id, timestamps in list(INFLIGHT_STATE.items()):
        active = [ts for ts in timestamps if ts >= stale_cutoff]
        if active:
            INFLIGHT_STATE[client_id] = active
        else:
            stale_clients.append(client_id)

    for client_id in stale_clients:
        INFLIGHT_STATE.pop(client_id, None)


def _global_inflight_count_locked():
    return sum(len(timestamps) for timestamps in INFLIGHT_STATE.values())


def _enter_request_guard(client_id):
    now_monotonic = time.monotonic()

    with SECURITY_LOCK:
        _prune_stale_locked(now_monotonic)

        global_count = _global_inflight_count_locked()
        if global_count >= settings.MAX_GLOBAL_CONCURRENT_REQUESTS:
            return None, _json_error(
                (
                    "houve um erro no servidor e "
                    "o conteúdo não pode ser mostrado"
                ),
                503,
            )

        client_timestamps = INFLIGHT_STATE.get(client_id, [])
        if len(client_timestamps) >= settings.MAX_CONCURRENT_REQUESTS_PER_ID:
            return None, _json_error(
                (
                    "muitas chamadas pendentes para este identificador; "
                    "tente novamente"
                ),
                503,
            )

        bucket = RATE_LIMIT_STATE.get(client_id)
        if not bucket:
            bucket = {"window_start": now_monotonic, "count": 0}
            RATE_LIMIT_STATE[client_id] = bucket

        elapsed = now_monotonic - float(bucket["window_start"])
        if elapsed >= settings.RATE_WINDOW_SECONDS:
            bucket["window_start"] = now_monotonic
            bucket["count"] = 0

        if bucket["count"] >= settings.MAX_REQUESTS_PER_ID_PER_MINUTE:
            return None, _json_error(
                "limite de chamadas excedido para este identificador",
                429,
            )

        bucket["count"] += 1
        client_timestamps.append(now_monotonic)
        INFLIGHT_STATE[client_id] = client_timestamps

    return now_monotonic, None


def _exit_request_guard(client_id, started_at):
    with SECURITY_LOCK:
        timestamps = INFLIGHT_STATE.get(client_id)
        if not timestamps:
            return

        for idx, timestamp in enumerate(timestamps):
            if timestamp == started_at:
                timestamps.pop(idx)
                break

        if timestamps:
            INFLIGHT_STATE[client_id] = timestamps
        else:
            INFLIGHT_STATE.pop(client_id, None)


@contextmanager
def _protected_request(request, body=None):
    if not _is_authorized(request):
        raise PermissionError("não autorizado")

    client_id = _extract_client_id(request, body)
    started_at, rejection = _enter_request_guard(client_id)
    if rejection is not None:
        yield rejection
        return

    try:
        yield None
    finally:
        _exit_request_guard(client_id, started_at)


def _parse_json_body(request):
    if not request.body:
        return {}
    try:
        body = json.loads(request.body)
    except (ValueError, RecursionError):
        return None
    return body if isinstance(body, dict) else None


def _json_error(message, status=400):
    return JsonResponse({"error": message}, status=status)


def _sanitize_relative_path(parts=None):
    if parts is None:
        return []
    if not isinstance(parts, list) or any(
        not isinstance(part, str)
        or not part
        or part in {".", ".."}
        or "/" in part
        or "\x00" in part
        for part in parts
    ):
        raise ValueError("path inválido")
    return parts


def _resolve_inside_workdir(workdir, rel_parts):
    base = workdir.resolve()
    resolved = workdir.joinpath(*rel_parts).resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError("path fora do workdir")
    return resolved


def _execution_environment():
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TERM": "xterm-256color",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run_command(command, cwd=None, timeout_ms=None, stdin_text=None):
    timeout_s = timeout_ms / 1000 if timeout_ms else None

    process = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        env=_execution_environment(),
        start_new_session=True,
    )

    try:
        stdout, stderr = process.communicate(
            input=stdin_text, timeout=timeout_s
        )
        return {
            "stdout": stdout or "",
            "stderr": stderr or "",
            "code": process.returncode,
            "timedOut": False,
        }
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        return {
            "stdout": stdout or "",
            "stderr": stderr or "",
            "code": 124,
            "timedOut": True,
        }


def _normalize_session_id(session_id):
    if session_id is None:
        return "legacy"
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(
        session_id
    ):
        raise ValueError("sessionId inválido")
    return session_id


def _project_session_key(project_id, session_id=None):
    return f"{_normalize_session_id(session_id)}:{project_id}"


def _active_web_session(project_id):
    session = WEB_SESSIONS.get(project_id)
    if session and session[0].poll() is None:
        return session
    WEB_SESSIONS.pop(project_id, None)
    return None


def _start_project_web(project_id, workdir, web):
    with WEB_LOCK:
        if _active_web_session(project_id):
            return web["url"]

        process = subprocess.Popen(
            web["command"],
            cwd=workdir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**_execution_environment(), "PYTHONUNBUFFERED": "1"},
        )
        try:
            for _ in range(30):
                if process.poll() is not None:
                    raise RuntimeError("o serviço web do projeto encerrou ao iniciar")
                try:
                    with urlopen(f'http://127.0.0.1:{web["port"]}', timeout=0.25):
                        WEB_SESSIONS[project_id] = (process, workdir)
                        return web["url"]
                except OSError:
                    time.sleep(0.2)
            raise RuntimeError("o serviço web do projeto não ficou pronto")
        except Exception:
            process.kill()
            process.wait()
            raise


def _ensure_project_prepared(project_id, session_id=None):
    project = settings.PROJECT_CATALOG[project_id]
    session_key = _project_session_key(project_id, session_id)
    with WORKSPACE_LOCK:
        existing = PROJECT_SESSIONS.get(session_key)
        if existing:
            return Path(existing)

    settings.WORKDIR_BASE.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(mkdtemp(prefix=f"{project_id}-", dir=str(settings.WORKDIR_BASE)))
    registered = False
    try:
        clone_result = _run_command(
            [
                "git", "clone", "--depth", "1", "--branch", project["branch"],
                "--", project["repoUrl"], str(temp_dir),
            ],
            timeout_ms=settings.CLONE_TIMEOUT_MS,
        )
        if clone_result["code"] != 0:
            detail = clone_result["stderr"] or clone_result["stdout"]
            raise RuntimeError(detail or "erro ao clonar")

        with WORKSPACE_LOCK:
            workspace = PROJECT_SESSIONS.setdefault(session_key, str(temp_dir))
            registered = workspace == str(temp_dir)
        return Path(workspace)
    finally:
        if not registered:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _resolve_project_command(
    workdir,
    rel_path,
    executable,
    args,
    runtime,
):
    target_dir = _resolve_inside_workdir(workdir, rel_path)
    if not target_dir.exists() or not target_dir.is_dir():
        raise ValueError("não é um diretório")

    normalized = executable.removeprefix("./")
    if "/" in normalized or normalized in {".", ".."}:
        raise ValueError("executável inválido")

    target_binary = _resolve_inside_workdir(
        workdir,
        [*rel_path, normalized],
    )
    if not target_binary.exists() or not target_binary.is_file():
        raise FileNotFoundError("executável não encontrado")
    if runtime == "native" and not os.access(target_binary, os.X_OK):
        raise ValueError("arquivo não tem permissão de execução")

    safe_args = [str(arg) for arg in args[:settings.MAX_RUN_ARGS]]
    if runtime == "python":
        if target_binary.suffix.lower() != ".py":
            raise ValueError("script Python inválido")
        command = ["python3", "-u", str(target_binary), *safe_args]
        command_label = " ".join(["python3", normalized, *safe_args])
    else:
        command = [str(target_binary), *safe_args]
        command_label = " ".join([f"./{normalized}", *safe_args])

    return target_dir, command, command_label


def _normalize_terminal_size(body):
    rows = body.get("rows", 40)
    cols = body.get("cols", 100)
    if not isinstance(rows, int) or not 12 <= rows <= 120:
        raise ValueError("quantidade de linhas inválida")
    if not isinstance(cols, int) or not 40 <= cols <= 240:
        raise ValueError("quantidade de colunas inválida")
    return rows, cols


@csrf_exempt
def health(request):
    if request.method != "GET":
        return _json_error("método não permitido", 405)

    return JsonResponse(
        {
            "ok": True,
            "service": "project-runner",
            "port": settings.PORT,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )


@csrf_exempt
def list_projects(request):
    if request.method != "GET":
        return _json_error("método não permitido", 405)
    try:
        with _protected_request(request) as rejection:
            if rejection is not None:
                return rejection

            return JsonResponse({"projects": list(settings.PROJECT_CATALOG.keys())})
    except PermissionError:
        return _json_error("não autorizado", 401)


@csrf_exempt
def prepare_project(request):
    if request.method != "POST":
        return _json_error("método não permitido", 405)

    body = _parse_json_body(request)
    if body is None:
        return _json_error("payload inválido")

    project_id = body.get("projectId")
    if not isinstance(project_id, str) or not project_id:
        return _json_error("projectId é obrigatório")

    try:
        with _protected_request(request, body) as rejection:
            if rejection is not None:
                return rejection

            workdir = _ensure_project_prepared(
                project_id, body.get("sessionId")
            )
            items = [
                {
                    "name": item.name,
                    "type": "dir" if item.is_dir() else "file",
                }
                for item in workdir.iterdir()
            ]
            return JsonResponse(
                {
                    "ok": True,
                    "projectId": project_id,
                    "workdir": str(workdir),
                    "items": items,
                }
            )
    except PermissionError:
        return _json_error("não autorizado", 401)
    except KeyError:
        return _json_error("projeto não encontrado", 404)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except Exception:
        logger.exception("prepare_project failed")
        return _safe_internal_error("erro ao preparar projeto")


@csrf_exempt
def list_project_fs(request):
    if request.method != "POST":
        return _json_error("método não permitido", 405)

    body = _parse_json_body(request)
    if body is None:
        return _json_error("payload inválido")

    project_id = body.get("projectId")
    if not isinstance(project_id, str) or not project_id:
        return _json_error("projectId é obrigatório")

    try:
        with _protected_request(request, body) as rejection:
            if rejection is not None:
                return rejection

            rel_path = _sanitize_relative_path(body.get("path", []))
            workdir = _ensure_project_prepared(
                project_id, body.get("sessionId")
            )
            target_dir = _resolve_inside_workdir(workdir, rel_path)

            if not target_dir.exists() or not target_dir.is_dir():
                return _json_error("não é um diretório")

            items = [
                {
                    "name": item.name,
                    "type": "dir" if item.is_dir() else "file",
                }
                for item in target_dir.iterdir()
            ]
            return JsonResponse(
                {
                    "ok": True,
                    "projectId": project_id,
                    "path": rel_path,
                    "items": items,
                }
            )
    except KeyError:
        return _json_error("projeto não encontrado", 404)
    except PermissionError:
        return _json_error("não autorizado", 401)
    except ValueError as exc:
        return _json_error(str(exc) or "path inválido", 400)
    except Exception:
        logger.exception("list_project_fs failed")
        return _safe_internal_error("erro ao listar diretório")


@csrf_exempt
def read_project_file(request):
    if request.method != "POST":
        return _json_error("método não permitido", 405)

    body = _parse_json_body(request)
    if body is None:
        return _json_error("payload inválido")

    project_id = body.get("projectId")
    file_name = body.get("file")

    if not isinstance(project_id, str) or not project_id:
        return _json_error("projectId é obrigatório")

    invalid_file = (
        not isinstance(file_name, str)
        or not file_name
        or "/" in file_name
        or file_name in {".", ".."}
    )
    if invalid_file:
        return _json_error("arquivo inválido")

    try:
        with _protected_request(request, body) as rejection:
            if rejection is not None:
                return rejection

            rel_path = _sanitize_relative_path(body.get("path", []))
            workdir = _ensure_project_prepared(
                project_id, body.get("sessionId")
            )
            target_dir = _resolve_inside_workdir(workdir, rel_path)
            target_file = _resolve_inside_workdir(
                workdir,
                [*rel_path, file_name],
            )

            if not target_dir.exists() or not target_dir.is_dir():
                return _json_error("diretório inválido")
            if not target_file.exists() or not target_file.is_file():
                return _json_error("não é um arquivo")
            if target_file.stat().st_size > settings.MAX_FILE_SIZE_BYTES:
                return _json_error("arquivo muito grande para visualização")

            suffix = target_file.suffix.lower()
            if suffix == ".pdf":
                raw_bytes = target_file.read_bytes()
                encoded = base64.b64encode(raw_bytes).decode("ascii")
                return JsonResponse(
                    {
                        "ok": True,
                        "projectId": project_id,
                        "path": rel_path,
                        "file": file_name,
                        "kind": "pdf",
                        "mimeType": "application/pdf",
                        "encoding": "base64",
                        "content": encoded,
                        "message": None,
                    }
                )

            try:
                content = target_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return JsonResponse(
                    {
                        "ok": True,
                        "projectId": project_id,
                        "path": rel_path,
                        "file": file_name,
                        "kind": "unsupported",
                        "mimeType": None,
                        "encoding": None,
                        "content": None,
                        "message": (
                            "tipo de arquivo não suportado para visualização"
                        ),
                    }
                )

            if suffix == ".md":
                kind = "markdown"
                mime_type = "text/markdown"
            elif suffix in CODE_EXTENSIONS:
                kind = "code"
                mime_type = "text/plain"
            else:
                kind = "text"
                mime_type = "text/plain"

            return JsonResponse(
                {
                    "ok": True,
                    "projectId": project_id,
                    "path": rel_path,
                    "file": file_name,
                    "kind": kind,
                    "mimeType": mime_type,
                    "encoding": "utf-8",
                    "content": content,
                    "message": None,
                }
            )
    except KeyError:
        return _json_error("projeto não encontrado", 404)
    except PermissionError:
        return _json_error("não autorizado", 401)
    except ValueError as exc:
        return _json_error(str(exc) or "path inválido", 400)
    except Exception:
        logger.exception("read_project_file failed")
        return _safe_internal_error("erro ao ler arquivo")


@csrf_exempt
def build_project(request):
    if request.method != "POST":
        return _json_error("método não permitido", 405)

    body = _parse_json_body(request)
    if body is None:
        return _json_error("payload inválido")

    project_id = body.get("projectId")
    args = body.get("args", [])

    if not isinstance(project_id, str) or not project_id:
        return _json_error("projectId é obrigatório")
    if not isinstance(args, list):
        return _json_error("args inválidos")

    invalid_arg = any(
        (not isinstance(arg, str) or not arg.strip()) for arg in args
    )
    if invalid_arg:
        return _json_error("args inválidos")
    if len(args) > settings.MAX_BUILD_ARGS:
        return _json_error("muitos argumentos para build")

    try:
        with _protected_request(request, body) as rejection:
            if rejection is not None:
                return rejection

            safe_args = [arg.strip() for arg in args if arg.strip()]
            rel_path = _sanitize_relative_path(body.get("path", []))
            workdir = _ensure_project_prepared(project_id, body.get("sessionId"))
            target_dir = _resolve_inside_workdir(workdir, rel_path)
            if not target_dir.is_dir():
                return _json_error("não é um diretório")

            web = settings.PROJECT_CATALOG[project_id].get("web")
            if web and safe_args == ["web"]:
                url = _start_project_web(project_id, workdir, web)
                return JsonResponse(
                    {"ok": True, "projectId": project_id, "webUrl": url}
                )

            result = _run_command(
                ["make", *safe_args],
                cwd=target_dir,
                timeout_ms=settings.BUILD_TIMEOUT_MS,
            )
            output = _combine_process_output(
                result["stdout"], result["stderr"]
            )

            return JsonResponse(
                {
                    "ok": True,
                    "projectId": project_id,
                    "path": rel_path,
                    "command": " ".join(["make", *safe_args]),
                    "exitCode": int(result["code"]),
                    "success": result["code"] == 0 and not result["timedOut"],
                    "timedOut": bool(result["timedOut"]),
                    "output": _truncate_output(output),
                }
            )
    except KeyError:
        return _json_error("projeto não encontrado", 404)
    except PermissionError:
        return _json_error("não autorizado", 401)
    except ValueError as exc:
        return _json_error(str(exc) or "path inválido", 400)
    except Exception:
        logger.exception("build_project failed")
        return _safe_internal_error("erro ao executar build")


@csrf_exempt
def run_project(request):
    if request.method != "POST":
        return _json_error("método não permitido", 405)

    body = _parse_json_body(request)
    if body is None:
        return _json_error("payload inválido")

    project_id = body.get("projectId")
    executable = body.get("executable")
    args = body.get("args", [])
    runtime = body.get("runtime", "native")
    stdin_text = body.get("stdin")

    if not isinstance(project_id, str) or not project_id:
        return _json_error("projectId é obrigatório")
    if not isinstance(executable, str) or not executable:
        return _json_error("executável é obrigatório")
    invalid_args = (
        not isinstance(args, list)
        or any(not isinstance(arg, str) for arg in args)
    )
    if invalid_args:
        return _json_error("args inválidos")
    if runtime not in ("native", "python"):
        return _json_error("runtime inválido")
    if stdin_text is not None and (
        not isinstance(stdin_text, str) or len(stdin_text) > 4096
    ):
        return _json_error("stdin inválido")

    try:
        with _protected_request(request, body) as rejection:
            if rejection is not None:
                return rejection

            rel_path = _sanitize_relative_path(body.get("path", []))
            workdir = _ensure_project_prepared(
                project_id, body.get("sessionId")
            )
            target_dir, command, command_label = _resolve_project_command(
                workdir,
                rel_path,
                executable,
                args,
                runtime,
            )
            result = _run_command(
                command,
                cwd=target_dir,
                timeout_ms=settings.PROJECT_CATALOG[project_id].get(
                    "runTimeoutMs", settings.RUN_TIMEOUT_MS
                ),
                stdin_text=stdin_text,
            )
            output = _combine_process_output(
                result["stdout"], result["stderr"]
            )
            return JsonResponse(
                {
                    "ok": True,
                    "projectId": project_id,
                    "path": rel_path,
                    "command": command_label,
                    "exitCode": int(result["code"]),
                    "success": result["code"] == 0 and not result["timedOut"],
                    "timedOut": bool(result["timedOut"]),
                    "output": _truncate_output(output),
                }
            )
    except KeyError:
        return _json_error("projeto não encontrado", 404)
    except PermissionError:
        return _json_error("não autorizado", 401)
    except FileNotFoundError as exc:
        return _json_error(str(exc), 400)
    except ValueError as exc:
        return _json_error(str(exc) or "path inválido", 400)
    except Exception:
        logger.exception("run_project failed")
        return _safe_internal_error("erro ao executar binário")


@csrf_exempt
def start_project_process(request):
    if request.method != "POST":
        return _json_error("método não permitido", 405)

    body = _parse_json_body(request)
    if body is None:
        return _json_error("payload inválido")

    project_id = body.get("projectId")
    executable = body.get("executable")
    args = body.get("args", [])
    runtime = body.get("runtime", "native")
    if not isinstance(project_id, str) or not project_id:
        return _json_error("projectId é obrigatório")
    if runtime != "bash" and (not isinstance(executable, str) or not executable):
        return _json_error("executável é obrigatório")
    if (
        not isinstance(args, list)
        or any(not isinstance(arg, str) for arg in args)
    ):
        return _json_error("args inválidos")
    if runtime not in ("native", "python", "bash"):
        return _json_error("runtime inválido")

    try:
        with _protected_request(request, body) as rejection:
            if rejection is not None:
                return rejection

            if project_id not in settings.PROJECT_CATALOG:
                return _json_error("projeto não encontrado", 404)
            if runtime == "bash":
                if not settings.RUNNER_SHARED_TOKEN or not settings.PROJECT_CATALOG[project_id].get("allowBash"):
                    return _json_error("Bash desabilitado para este projeto", 403)
                if executable is not None or args:
                    return _json_error("runtime bash não recebe executable ou args")

            rel_path = _sanitize_relative_path(body.get("path", []))
            rows, cols = _normalize_terminal_size(body)
            workdir = _ensure_project_prepared(
                project_id, body.get("sessionId")
            )
            if runtime == "bash":
                target_dir = _resolve_inside_workdir(workdir, rel_path)
                if not target_dir.is_dir():
                    return _json_error("não é um diretório")
                command = ["bash", "--noprofile", "--norc", "-i"]
                command_label = "bash"
            else:
                target_dir, command, command_label = _resolve_project_command(
                    workdir, rel_path, executable, args, runtime,
                )
            session_key = _project_session_key(
                project_id, body.get("sessionId")
            )
            process_env = _execution_environment()
            process_env.update(
                {
                    "COLUMNS": str(cols),
                    "LINES": str(rows),
                    "PYTHONUNBUFFERED": "1",
                }
            )
            PTY_MANAGER.start(
                session_key,
                command,
                target_dir,
                rows,
                cols,
                process_env,
            )
            return JsonResponse(
                {
                    "ok": True,
                    "projectId": project_id,
                    "path": rel_path,
                    "command": command_label,
                    "running": True,
                }
            )
    except PermissionError:
        return _json_error("não autorizado", 401)
    except FileNotFoundError as exc:
        return _json_error(str(exc), 400)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except Exception:
        logger.exception("start_project_process failed")
        return _safe_internal_error("erro ao iniciar processo interativo")


@csrf_exempt
def poll_project_process(request):
    if request.method != "POST":
        return _json_error("método não permitido", 405)
    if not _is_authorized(request):
        return _json_error("não autorizado", 401)

    body = _parse_json_body(request)
    if body is None:
        return _json_error("payload inválido")
    project_id = body.get("projectId")
    if not isinstance(project_id, str) or project_id not in settings.PROJECT_CATALOG:
        return _json_error("projeto não encontrado", 404)

    try:
        session_key = _project_session_key(
            project_id, body.get("sessionId")
        )
        result = PTY_MANAGER.poll(session_key)
        if result is None:
            return _json_error("processo interativo não encontrado", 404)
        output = result.pop("output")
        return JsonResponse(
            {
                "ok": True,
                **result,
                "outputBase64": base64.b64encode(output).decode("ascii"),
            }
        )
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except Exception:
        logger.exception("poll_project_process failed")
        return _safe_internal_error("erro ao consultar processo interativo")


@csrf_exempt
def input_project_process(request):
    if request.method != "POST":
        return _json_error("método não permitido", 405)
    if not _is_authorized(request):
        return _json_error("não autorizado", 401)

    body = _parse_json_body(request)
    if body is None:
        return _json_error("payload inválido")
    project_id = body.get("projectId")
    data = body.get("data")
    if not isinstance(project_id, str) or project_id not in settings.PROJECT_CATALOG:
        return _json_error("projeto não encontrado", 404)
    if not isinstance(data, str):
        return _json_error("input inválido")
    encoded = data.encode("utf-8")
    if not encoded or len(encoded) > settings.MAX_PTY_INPUT_BYTES:
        return _json_error("input inválido")

    try:
        session_key = _project_session_key(
            project_id, body.get("sessionId")
        )
        if not PTY_MANAGER.write(session_key, encoded):
            return _json_error("processo interativo não está ativo", 409)
        return JsonResponse({"ok": True})
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except Exception:
        logger.exception("input_project_process failed")
        return _safe_internal_error("erro ao enviar input")


@csrf_exempt
def resize_project_process(request):
    if request.method != "POST":
        return _json_error("método não permitido", 405)

    body = _parse_json_body(request)
    if body is None:
        return _json_error("payload inválido")
    project_id = body.get("projectId")
    if not isinstance(project_id, str) or project_id not in settings.PROJECT_CATALOG:
        return _json_error("projeto não encontrado", 404)

    try:
        with _protected_request(request, body) as rejection:
            if rejection is not None:
                return rejection
            rows, cols = _normalize_terminal_size(body)
            session_key = _project_session_key(
                project_id, body.get("sessionId")
            )
            if not PTY_MANAGER.resize(session_key, rows, cols):
                return _json_error("processo interativo não está ativo", 409)
            return JsonResponse({"ok": True, "rows": rows, "cols": cols})
    except PermissionError:
        return _json_error("não autorizado", 401)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except Exception:
        logger.exception("resize_project_process failed")
        return _safe_internal_error("erro ao redimensionar terminal")


@csrf_exempt
def cleanup_project(request):
    if request.method != "DELETE":
        return _json_error("método não permitido", 405)

    body = _parse_json_body(request)
    if body is None:
        return _json_error("payload inválido")

    project_id = body.get("projectId")
    workdir = body.get("workdir")

    try:
        with _protected_request(request, body) as rejection:
            if rejection is not None:
                return rejection

            with WORKSPACE_LOCK, WEB_LOCK:
                if isinstance(project_id, str) and project_id:
                    session_key = _project_session_key(project_id, body.get("sessionId"))
                elif isinstance(workdir, str) and workdir:
                    session_key = next(
                        (key for key, path in PROJECT_SESSIONS.items() if path == workdir),
                        None,
                    )
                    if session_key is None:
                        return _json_error("workdir inválido")
                    project_id = session_key.split(":", 1)[1]
                else:
                    return _json_error("projectId ou workdir é obrigatório")

                PTY_MANAGER.stop(session_key)
                existing = PROJECT_SESSIONS.get(session_key)
                if not existing:
                    return JsonResponse({"ok": True, "deleted": None})

                web_session = _active_web_session(project_id)
                if web_session and web_session[1] == Path(existing):
                    return JsonResponse({"ok": True, "deleted": None, "retained": existing})

                shutil.rmtree(existing)
                PROJECT_SESSIONS.pop(session_key)
                return JsonResponse({"ok": True, "deleted": existing})
    except PermissionError:
        return _json_error("não autorizado", 401)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except Exception:
        logger.exception("cleanup_project failed")
        return _safe_internal_error("erro ao limpar workdir")
