"""LLM providers: which endpoint, which credential, which model.

Every provider here speaks OpenAI's chat-completions protocol, so there is no
per-provider adapter code — DeepSeek and DeepInfra are llama-index's *same*
`OpenAILike` class with a different `api_base`. What this module owns is the
provider-specific configuration around that: the endpoint, the env var that
authenticates it, the default model id, and the shorthands for naming one.

`PROVIDERS` is the single table `get_llm`, the preflight and the error help all
read, so adding a provider is one entry rather than an edit in five places.

Deliberately depends on nothing in `agent.py`: the agent injects its
transcript-logging class factory as `wrap` (see `get_llm`), which keeps this
module importable and testable on its own and avoids an import cycle.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

#: hard bound on one LLM request, so a hung endpoint surfaces as a timeout
#: error instead of an open connection the operator has to Ctrl-C out of
LLM_TIMEOUT = 120

#: DeepSeek's canonical id for V4.1 Flash. DeepSeek still accepts the older
#: aliases (`deepseek-v4-flash`, `deepseek-chat`) and serves this same model for
#: them, but `deepseek-v4.1-flash` is rejected outright — /v1/models lists only
#: `deepseek-flash` and `deepseek-v4-pro`.
DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"

#: Short `--model` names, so `--model pro` / `--model flash` work without the
#: caller having to know that the flash id dropped its version number.
DEEPSEEK_MODEL_ALIASES = {
    "flash": "deepseek-flash",
    "pro": "deepseek-v4-pro",
}

#: DeepInfra's GLM-5.3 flagship. The full id is required — see `_deepinfra_llm`.
DEFAULT_DEEPINFRA_MODEL = "zai-org/GLM-5.3"

#: DeepInfra ids we have actually checked against `/v1/models`, id -> what it is
#: for. A discovery aid, **not** an allow-list: `--model` still accepts anything
#: the provider serves, because DeepInfra fronts a large third-party catalogue
#: and we keep no alias table for it. Notes stay qualitative on purpose —
#: context windows and prices move on the provider's schedule, so pinning
#: numbers here would only go stale. `--doctor` prints this list.
DEEPINFRA_KNOWN_MODELS = {
    "zai-org/GLM-5.3": "GLM-5.3 flagship (the default)",
    "zai-org/GLM-5.3-Flash": "GLM-5.3 flash tier; cheaper, 1M context",
    "Qwen/Qwen3.8-Flash": "Qwen3.8 fast/low-cost tier; 1M context",
    "Qwen/Qwen3.8-27B": "Qwen3.8 27B dense VLM + reasoning; pricier output",
}


@dataclass(frozen=True)
class Provider:
    """Where a provider serves, and which env var authenticates it."""

    api_base: str
    key_env: str


#: The provider table. `get_llm`, `_llm_base`, `_preflight_llm` and the error
#: help all read it, so provider knowledge lives in exactly one place.
PROVIDERS = {
    "deepseek": Provider("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "deepinfra": Provider("https://api.deepinfra.com/v1/openai",
                          "DEEPINFRA_API_KEY"),
    "openai": Provider("https://api.openai.com/v1", "OPENAI_API_KEY"),
}

#: The order `--provider auto` (the default) resolves in. Must match the
#: preference `get_llm` applies.
PROVIDER_ORDER = ("openai", "deepseek", "deepinfra")


def effective_provider(provider: str) -> str:
    """Resolve `auto` to the first provider whose key is set; `""` when none is.

    An explicit provider is returned as-is (or `""` if it is not one we know),
    so a caller that names a provider never silently gets a different one.
    """
    if provider != "auto":
        return provider if provider in PROVIDERS else ""
    for name in PROVIDER_ORDER:
        if os.environ.get(PROVIDERS[name].key_env):
            return name
    return ""


def api_base_for(provider: str, api_base: str = "") -> str:
    """The endpoint a run will use: the explicit flag, else the provider's own.

    This is what makes the preflight honest — previously it assumed DeepSeek, so
    a DeepInfra run probed DeepSeek's host with a key DeepInfra would never use.
    """
    if api_base:
        return api_base
    resolved = PROVIDERS.get(effective_provider(provider))
    # an unresolved provider is already a hard error before a run starts; keep
    # the historical DeepSeek base as the harmless last resort
    return resolved.api_base if resolved else PROVIDERS["deepseek"].api_base


def resolve_deepseek_model(model: str) -> str:
    """Map a DeepSeek shorthand to its canonical id; pass anything else through.

    Only `flash`/`pro` (case- and whitespace-insensitive) are rewritten, so
    `--model` can still name any model DeepSeek serves — a full id is never
    second-guessed. An empty name means "provider default".
    """
    name = (model or "").strip()
    return DEEPSEEK_MODEL_ALIASES.get(name.lower()) or name or DEFAULT_DEEPSEEK_MODEL


def _unwrapped(cls):
    """The default `wrap`: no instrumentation at all.

    The agent passes its own factory so every remote call lands in its run
    transcript; standing alone, this module builds the bare llama-index class.
    """
    return cls


def _deepseek_llm(model: str, api_base: str = "", wrap=None):
    """OpenAI-compatible client for DeepSeek.

    Uses llama-index's OpenAILike (not the OpenAI class, whose metadata
    property validates model names against OpenAI's registry and rejects
    DeepSeek model ids). is_function_calling_model=True is required for the
    FunctionAgent tool loop.

    The default is `DEFAULT_DEEPSEEK_MODEL` (V4.1 Flash); see
    `resolve_deepseek_model` for the `flash`/`pro` shorthands.
    """
    from llama_index.llms.openai_like import OpenAILike

    spec = PROVIDERS["deepseek"]
    return (wrap or _unwrapped)(OpenAILike)(
        model=resolve_deepseek_model(model),
        api_key=os.environ.get(spec.key_env),
        api_base=api_base or spec.api_base,
        is_chat_model=True,
        is_function_calling_model=True,
        # fail fast and loud: no silent SDK retries, and a hard read bound so a
        # hung request surfaces as APITimeoutError instead of an open connection
        timeout=LLM_TIMEOUT,
        max_retries=0,
    )


def _deepinfra_llm(model: str, api_base: str = "", wrap=None):
    """OpenAI-compatible client for DeepInfra (GLM and friends).

    `--model` takes the provider's **full** id, namespace included, e.g.
    `zai-org/GLM-5.3-Flash` or `zai-org/GLM-5.3`. There are deliberately no
    shorthands here: DeepInfra fronts a large third-party catalogue whose ids
    are free to change, so an alias table of ours would only be a second, drifting
    name for something the provider already names unambiguously. The default is
    the GLM-5.3 flagship, spelled in full.
    """
    from llama_index.llms.openai_like import OpenAILike

    spec = PROVIDERS["deepinfra"]
    return (wrap or _unwrapped)(OpenAILike)(
        model=(model or "").strip() or DEFAULT_DEEPINFRA_MODEL,
        api_key=os.environ.get(spec.key_env),
        api_base=api_base or spec.api_base,
        is_chat_model=True,
        is_function_calling_model=True,
        timeout=LLM_TIMEOUT,
        max_retries=0,
    )


def _openai_llm(model: str, wrap=None):
    """OpenAI LLM, wrapped by the agent so its calls are logged like the rest."""
    from llama_index.llms.openai import OpenAI

    return (wrap or _unwrapped)(OpenAI)(model=model or "gpt-4o-mini",
                                        timeout=LLM_TIMEOUT, max_retries=0)


def get_llm(provider: str, model: str = "", api_base: str = "", wrap=None):
    """Build the LLM client for `provider`, or explain that none is configured.

    `wrap` is a class factory applied to the llama-index LLM class — the agent
    passes `instrumented_llm` so calls are journalled. It defaults to no
    instrumentation, which is what keeps this module free of agent imports.
    """
    if provider in ("deepseek", "deepinfra"):
        # an explicitly named provider that has no key is worth its own message
        # rather than a client that fails on the first call
        key_env = PROVIDERS[provider].key_env
        if not os.environ.get(key_env):
            raise SystemExit(
                f"{key_env} is not set. export {key_env}=... "
                "(or pass --provider openai)"
            )

    resolved = effective_provider(provider)
    if resolved == "openai":
        return _openai_llm(model, wrap)
    if resolved == "deepseek":
        return _deepseek_llm(model, api_base, wrap)
    if resolved == "deepinfra":
        return _deepinfra_llm(model, api_base, wrap)
    raise SystemExit(
        "No LLM provider configured. Set OPENAI_API_KEY, DEEPSEEK_API_KEY "
        "or DEEPINFRA_API_KEY and choose --provider openai|deepseek|deepinfra."
    )


def llm_preflight(api_base: str, api_key: str, *, timeout: int = 10,
                  resolve=None, open_url=None) -> tuple[bool, str]:
    """Probe the LLM endpoint so a network problem is reported clearly.

    The OpenAI SDK wraps *every* transport failure — DNS, TLS verification,
    a dead proxy, connection refused — in a bare `APIConnectionError` with no
    hint about which. This resolves the host, does a real request, and reports
    the HTTP status: 401/403 means the endpoint is reachable, so the problem is
    the key rather than the network.

    `resolve`/`open_url` are injection seams so this can be unit-tested offline.
    """
    import socket
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse

    resolve = resolve or socket.getaddrinfo
    open_url = open_url or urllib.request.urlopen

    url = api_base if "://" in api_base else f"https://{api_base}"
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    proxies = {k: v for k, v in os.environ.items()
               if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}

    try:
        infos = resolve(host, port, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
    except Exception as e:  # noqa: BLE001 — DNS failure is the whole point
        return False, (f"DNS lookup failed for {host}: {type(e).__name__}: {e}\n"
                       f"  proxies in env: {proxies or 'none'}")

    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        with open_url(req, timeout=timeout) as resp:
            return True, f"{url} reachable (HTTP {resp.status})"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return True, (f"{url} reachable (HTTP {e.code}) — network is fine, "
                          "check the API key")
        if e.code == 404:
            # DNS, TCP and TLS all succeeded — an API root routinely has no
            # handler, so blaming --api-base here is misleading noise.
            return True, (f"{url} reachable (HTTP 404 at the root — normal for "
                          "many API hosts)")
        return True, (f"{url} reachable (HTTP {e.code}) — if calls also fail, "
                      "check --api-base")
    except Exception as e:  # noqa: BLE001
        return False, (f"cannot reach {url}: {type(e).__name__}: {e}\n"
                       f"  resolved {host} -> {', '.join(ips)}\n"
                       f"  proxies in env: {proxies or 'none'}")


def describe_llm_error(exc: BaseException, api_base: str, model: str = "",
                       provider: str = "") -> str:
    """Turn any LLM failure into an actionable help block.

    Classifies by HTTP status first, then by class name, because the OpenAI SDK
    raises the *same* `APIConnectionError` for DNS failure, TLS verification
    errors, a dead proxy and a refused connection. Always prints the endpoint and
    model actually in use, then concrete next steps.
    """
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    raw = " ".join(str(exc).split())[:300]
    low = f"{name} {raw}".lower()
    curl = "curl -sS -m 5 -o /dev/null -w '%{http_code}\\n' " + api_base + "/"

    if status == 401 or "authenticationerror" in low:
        cause = "The API key is missing, wrong, or revoked (HTTP 401)."
        steps = ["export DEEPSEEK_API_KEY=... then re-run",
                 "Or use --provider openai with OPENAI_API_KEY."]
    elif status == 403 or "permissiondenied" in low:
        cause = "The key is valid but not allowed to use this model (HTTP 403)."
        steps = ["Check the key's project/scope, or change --model."]
    elif status == 404 or "notfounderror" in low:
        cause = "Endpoint or model not found (HTTP 404)."
        steps = [f"Model in use: {model or '(provider default)'} — check the exact id.",
                 "For DeepSeek use: --api-base https://api.deepseek.com"]
    elif status == 429 or "ratelimit" in low:
        cause = "Rate limited or out of quota (HTTP 429)."
        steps = ["Retry shortly; lower --max-rounds; check the account balance."]
    elif status == 400 or "badrequesterror" in low:
        cause = ("The request was rejected (HTTP 400) — usually context length, "
                 "or a tool schema the model refuses.")
        steps = ["Try one smaller sheet, or a model with a larger context window."]
    elif isinstance(status, int) and 500 <= status < 600:
        cause = f"The provider failed server-side (HTTP {status})."
        steps = ["Retry; if it persists, try --provider openai."]
    elif any(k in low for k in ("connection", "connect", "timeout", "ssl",
                                "proxy", "unreachable", "getaddrinfo")):
        cause = ("Network/transport failure — DNS, TLS verification, a dead proxy "
                 "or a firewall. The SDK reports all of these identically.")
        steps = ["env | grep -iE 'proxy'    # a stale HTTPS_PROXY is the usual cause",
                 curl,
                 "401 from curl = network works (so the key is the problem);",
                 "no response at all = blocked by DNS/VPN/firewall.",
                 "Or pass --api-base <url>, or --provider openai."]
    else:
        cause = "Unexpected LLM failure."
        steps = ["Re-run with AGENT_DEBUG=1 to see the full traceback."]

    out = [f"LLM call failed — {name}: {raw}", "",
           f"  endpoint : {api_base}",
           f"  model    : {model or '(provider default)'}"]
    if provider:
        out.append(f"  provider : {provider}")
    out += ["", f"  {cause}", "", "  Next steps:"]
    out += [f"    {s}" for s in steps]
    out += ["", "  No LLM needed (builds every analysis offline):",
            "    DOCTOR=1 scripts/run-all-agentic.sh"]
    return "\n".join(out)


def _is_llm_error(exc: BaseException) -> bool:
    """True for OpenAI/httpx API failures, as opposed to a bug in our own code."""
    if type(exc).__module__.split(".")[0] in ("openai", "httpx", "httpcore"):
        return True
    name = type(exc).__name__.lower()
    return any(k in name for k in ("apierror", "connection", "timeout", "ratelimit",
                                   "authentication", "permissiondenied", "notfound"))


def _llm_base(args) -> str:
    """The endpoint this run will use, from the parsed CLI namespace."""
    return api_base_for(args.provider, args.api_base)


def _preflight_llm(args) -> bool:
    """Probe the endpoint once, so a network problem is not a mid-loop traceback.

    Probes the host the run will actually use, with the credential that host
    will actually get. Assuming DeepSeek here used to mean a DeepInfra run was
    gated on DeepSeek's reachability and reported DeepSeek's status against a
    key it would never send.
    """
    provider = effective_provider(args.provider)
    if provider == "openai":
        return True
    base = _llm_base(args)
    key_env = PROVIDERS[provider].key_env if provider in PROVIDERS else ""
    ok, detail = llm_preflight(base, os.environ.get(key_env, ""))
    if not ok:
        # the probe knows *why*; the shared help block knows the next steps
        print(f"ERROR: LLM endpoint unreachable — {detail}", file=sys.stderr)
        print(describe_llm_error(ConnectionError(detail), base,
                                 args.model or "", args.provider), file=sys.stderr)
        return False
    print(f"LLM preflight: {detail}")
    return True
