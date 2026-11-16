"""Validate server settings without connecting to services or printing secrets."""

import ipaddress
import json
import re
from urllib.parse import urlsplit

from rag._configuration import environment_values
from rag.ingestion._gateway_settings import _GatewaySettings
from rag.ingestion._settings import _Settings as IngestionSettings
from rag.ingestion._chunking import _ChunkSettings as ChunkPolicy
from rag.rag_api.config import get_settings
from rag.runtime._config import RuntimeSettings
from rag.runtime.runner import _bindings


def public_host(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if (len(value) > 253 or not re.fullmatch(
                r"(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
                r"[a-zA-Z](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", value)):
            raise ValueError("PUBLIC_HOST must be a public IPv4 address or DNS name") from None
        if value.endswith((".invalid", ".example", ".test", ".localhost")):
            raise ValueError("PUBLIC_HOST must be a real server address")
        return value.lower()
    if address.version != 4 or not address.is_global:
        raise ValueError("PUBLIC_HOST must be a public IPv4 address")
    return str(address)


def active_bindings(settings):
    selected = {**_bindings("questions", settings), **_bindings("ingestion", settings)}
    all_bindings = json.loads(settings.model_bindings.get_secret_value())
    from pydantic import TypeAdapter
    from rag.model_gateway.contracts import ModelBinding, ModelRole

    validated = TypeAdapter(dict[ModelRole, ModelBinding]).validate_python(all_bindings)
    selected.update({role: binding for role, binding in validated.items() if role in {"ocr", "vision"}})
    return selected


def validate():
    values = environment_values()
    if values.get('TELEGRAM_UPDATE_MODE', 'polling') != 'polling':
        raise ValueError('Only polling is supported; remove TELEGRAM_UPDATE_MODE or set polling')
    host = public_host(values.get("PUBLIC_HOST", ""))
    api = get_settings()
    if api.public_url != "https://" + host:
        raise ValueError("RAG_PUBLIC_URL must equal https://PUBLIC_HOST, without a path")
    api.intake_secret  # Required for the poller, independently of Mini App auth.
    settings = RuntimeSettings()
    worker = IngestionSettings.from_environment()
    ChunkPolicy.from_environment()
    gateway = _GatewaySettings()
    required = (settings.pg_password, settings.rabbitmq_password, settings.s3_secret_key,
                settings.admin_login, settings.admin_password, gateway.token)
    if any(not value.get_secret_value().strip() for value in required):
        raise ValueError("Database, S3, administrative and ingestion credentials must be nonempty")
    if not values.get("GRAFANA_PASSWORD", "").strip():
        raise ValueError("GRAFANA_PASSWORD must be nonempty")
    bindings = active_bindings(settings)
    if (worker.embedding_profile != settings.embedding_profile
            or worker.embedding_dimensions != settings.embedding_dimensions
            or worker.generation_profile != bindings["generation"].profile_id
            or worker.preparation_profile != settings.preparation_profile):
        raise ValueError("Core, ingestion and model profiles must match")
    if settings.use_gigachat:
        from rag.model_gateway._gigachat_config import _Options

        options = _Options()
        options._binding()
        if not values.get("GIGACHAT_CREDENTIALS", "").strip():
            raise ValueError("RAG_USE_GIGACHAT requires GIGACHAT_CREDENTIALS")
        if (options.verify_ssl_certs and options.ca_bundle is not None
                and not options.ca_bundle.is_file()):
            raise ValueError("GIGACHAT_CA_BUNDLE must point to a readable PEM file")
    revisions = {}
    from rag.model_gateway.ollama import _model_name
    for binding in bindings.values():
        if binding.provider == "injection_detector" and binding.execution == "remote":
            if binding != bindings.get("injection") or binding.endpoint != "http://injection-detector:8080" or binding.credential_env:
                raise ValueError("Injection detector must use its private endpoint without a credential_env")
            if "native" not in values.get("COMPOSE_PROFILES", "").split(","):
                raise ValueError("Injection detector requires COMPOSE_PROFILES=native")
            continue
        if binding.provider == "native_cpu" and binding.execution == "remote":
            if binding.endpoint != "http://native-models:8090":
                raise ValueError("Native CPU bindings must use http://native-models:8090")
            if binding.credential_env != "NATIVE_MODEL_TOKEN" or not values.get("NATIVE_MODEL_TOKEN"):
                raise ValueError("Native CPU models require NATIVE_MODEL_TOKEN")
            if "native" not in values.get("COMPOSE_PROFILES", "").split(","):
                raise ValueError("Native CPU models require COMPOSE_PROFILES=native")
            continue
        if binding.provider != "ollama" or binding.execution != "remote":
            raise ValueError("Unsupported server model provider")
        endpoint = urlsplit(binding.endpoint)
        if (endpoint.scheme != "http" or endpoint.hostname != "ollama"
                or endpoint.port != 11434 or endpoint.path not in ("", "/")
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError("Ollama bindings must use http://ollama:11434")
        revision = binding.model_revision.removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", revision):
            raise ValueError("Every Ollama model must have a full SHA-256 revision")
        previous = revisions.setdefault(_model_name(binding.model_id), revision)
        if previous != revision:
            raise ValueError("Roles using the same model must pin the same revision")
    modules = json.loads(values.get("MODEL_GATEWAY_PROVIDER_MODULES", "[]"))
    if "rag.model_gateway.ollama" not in modules:
        raise ValueError("MODEL_GATEWAY_PROVIDER_MODULES must register the Ollama adapter")
    if any(binding.provider == "native_cpu" for binding in bindings.values()) and "rag.model_gateway.native_cpu" not in modules:
        raise ValueError("MODEL_GATEWAY_PROVIDER_MODULES must register the native CPU adapter")
    if any(binding.provider == "injection_detector" for binding in bindings.values()) and "rag.model_gateway.injection_detector" not in modules:
        raise ValueError("MODEL_GATEWAY_PROVIDER_MODULES must register the injection detector adapter")
    from rag.rag_core._settings import _Settings as CoreSettings
    from rag.rag_core.answer_generator import _SYSTEM
    from rag.model_gateway.ollama import _Options
    core = CoreSettings()
    if core.max_input_bytes < len(_SYSTEM.encode("utf-8")) + 512:
        raise ValueError("RAG_CORE_MAX_INPUT_BYTES leaves no room for document fragments after the system prompt")
    if bindings["generation"].provider == "ollama" and core.max_input_bytes + core.max_output_tokens + 192 > _Options().text_context_length:
        raise ValueError("OLLAMA_GATEWAY_TEXT_CONTEXT_LENGTH must cover Core input, output and protocol overhead")
    if int(values.get("INGESTION_PROXY_TIMEOUT_S", "660")) <= gateway.upload_timeout_s:
        raise ValueError("INGESTION_PROXY_TIMEOUT_S must exceed the ingestion upload timeout")
    for key in ("API_WORKERS", "INGESTION_HTTP_WORKERS"):
        if not 1 <= int(values.get(key, "2" if key == "API_WORKERS" else "1")) <= 16:
            raise ValueError(key + " must be between 1 and 16")
    usernames = json.loads(values.get("RAG_BOOTSTRAP_USERNAMES", "[]"))
    if (not isinstance(usernames, list)
            or any(not isinstance(name, str) or not re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", name)
                   for name in usernames)):
        raise ValueError("RAG_BOOTSTRAP_USERNAMES must be a JSON array of Telegram usernames")
    return settings
