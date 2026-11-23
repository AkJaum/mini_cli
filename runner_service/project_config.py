import json
import re
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

PROJECT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _is_positive_int(value):
    return type(value) is int and value > 0


def _valid_repo_url(value):
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        return False

    try:
        url = urlsplit(value)
        if url.scheme == "file":
            return not url.netloc and url.path.startswith("/")
        return (
            url.scheme == "https"
            and bool(url.hostname and url.path)
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
        )
    except ValueError:
        return False


def _valid_web_config(web):
    if not isinstance(web, dict) or set(web) != {"command", "port", "url"}:
        return False
    command = web["command"]
    port = web["port"]
    url = web["url"]
    return (
        isinstance(command, list)
        and bool(command)
        and all(isinstance(arg, str) and arg and "\x00" not in arg for arg in command)
        and _is_positive_int(port)
        and port <= 65535
        and isinstance(url, str)
        and url.startswith(("https://", "http://"))
    )


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"chave duplicada: {key}")
        result[key] = value
    return result


def load_project_catalog(path):
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys
        )
    except FileNotFoundError as exc:
        raise ImproperlyConfigured(
            f"{path} não encontrado. Copie .config.example.json para config.json."
        ) from exc
    except (OSError, ValueError) as exc:
        raise ImproperlyConfigured(f"Não foi possível ler {path}: {exc}") from exc

    if not isinstance(document, dict) or set(document) != {"projects"}:
        raise ImproperlyConfigured(f"{path}: esperado um objeto com a chave projects")
    projects = document["projects"]
    if not isinstance(projects, dict):
        raise ImproperlyConfigured(f"{path}: projects deve ser um objeto")

    web_ports = set()
    for ref, project in projects.items():
        prefix = f"{path}: projects.{ref}"
        if not PROJECT_ID_PATTERN.fullmatch(ref):
            raise ImproperlyConfigured(f"{prefix}: referência inválida")
        if not isinstance(project, dict):
            raise ImproperlyConfigured(f"{prefix}: esperado um objeto")

        unknown = project.keys() - {
            "repoUrl",
            "branch",
            "runTimeoutMs",
            "allowBash",
            "web",
        }
        if unknown:
            raise ImproperlyConfigured(
                f"{prefix}: campos desconhecidos: {', '.join(sorted(unknown))}"
            )
        if not _valid_repo_url(project.get("repoUrl")):
            raise ImproperlyConfigured(
                f"{prefix}.repoUrl: use uma URL HTTPS ou file:// absoluta"
            )
        branch = project.get("branch")
        if (
            not isinstance(branch, str)
            or not branch
            or branch.startswith("-")
            or any(c.isspace() or ord(c) < 32 for c in branch)
        ):
            raise ImproperlyConfigured(f"{prefix}.branch: informe o nome do branch")
        if "runTimeoutMs" in project and not _is_positive_int(project["runTimeoutMs"]):
            raise ImproperlyConfigured(
                f"{prefix}.runTimeoutMs: esperado um inteiro positivo"
            )
        if "allowBash" in project and not isinstance(project["allowBash"], bool):
            raise ImproperlyConfigured(f"{prefix}.allowBash: esperado true ou false")
        if "web" in project:
            web = project["web"]
            if not _valid_web_config(web):
                raise ImproperlyConfigured(
                    f"{prefix}.web: informe command, port e url válidos"
                )
            if web["port"] in web_ports:
                raise ImproperlyConfigured(f"{prefix}.web.port: porta já configurada")
            web_ports.add(web["port"])

    return projects
