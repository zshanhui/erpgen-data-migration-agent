"""P0: LLM endpoint preflight.

The OpenAI SDK reports DNS failures, TLS verification errors, a dead proxy and a
refused connection *identically* — as a bare `APIConnectionError` with no clue
which. `run_agent` therefore probes the endpoint first and turns that into an
actionable message. Pure: the network calls are injected through the
`resolve` / `open_url` seams, so none of this touches the network.
"""
from __future__ import annotations

import importlib.util
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def agent_mod():
    """scripts/agent.py is a script, not an importable package module."""
    if "agent_under_test" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "agent_under_test", ROOT / "scripts" / "agent.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["agent_under_test"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["agent_under_test"]


# ------------------------------------------------------------------- fakes
def _resolve_ok(host, port, **kw):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def _resolve_fail(host, port, **kw):
    raise OSError(8, "nodename nor servname provided, or not known")


class _Resp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _open_status(code):
    def _open(req, timeout=None):
        if code >= 400:
            raise urllib.error.HTTPError(req.full_url, code, "err", {}, None)
        return _Resp(code)
    return _open


def _open_raises(exc):
    def _open(req, timeout=None):
        raise exc
    return _open


# ------------------------------------------------------------------- tests
def test_dns_failure_is_reported_with_proxy_hints(agent_mod, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    ok, detail = agent_mod.llm_preflight("https://api.deepseek.com", "k",
                                         resolve=_resolve_fail)
    assert ok is False
    assert "DNS lookup failed" in detail
    assert "127.0.0.1:9" in detail, "proxy env vars must be surfaced"


def test_reachable_401_means_the_key_is_the_problem(agent_mod, monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    ok, detail = agent_mod.llm_preflight("https://api.deepseek.com", "k",
                                         resolve=_resolve_ok,
                                         open_url=_open_status(401))
    assert ok is True, "401 proves the network path works"
    assert "check the API key" in detail


def test_reachable_403_is_also_a_key_problem(agent_mod):
    ok, detail = agent_mod.llm_preflight("https://api.deepseek.com", "k",
                                         resolve=_resolve_ok,
                                         open_url=_open_status(403))
    assert ok is True and "check the API key" in detail


def test_404_points_at_api_base(agent_mod):
    ok, detail = agent_mod.llm_preflight("https://api.deepseek.com", "k",
                                         resolve=_resolve_ok,
                                         open_url=_open_status(404))
    assert ok is True
    assert "--api-base" in detail


def test_200_is_reachable(agent_mod):
    ok, detail = agent_mod.llm_preflight("https://api.deepseek.com", "k",
                                         resolve=_resolve_ok,
                                         open_url=_open_status(200))
    assert ok is True and "HTTP 200" in detail


def test_transport_error_reports_resolved_ips(agent_mod, monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    ok, detail = agent_mod.llm_preflight(
        "https://api.deepseek.com", "k", resolve=_resolve_ok,
        open_url=_open_raises(OSError("connection refused")))
    assert ok is False
    assert "93.184.216.34" in detail, "the IP actually reached must be shown"
    assert "proxies in env: none" in detail


def test_bare_host_is_upgraded_to_https(agent_mod):
    seen = {}

    def _open(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp(200)

    agent_mod.llm_preflight("api.deepseek.com", "k", resolve=_resolve_ok,
                            open_url=_open)
    assert seen["url"] == "https://api.deepseek.com"


def test_api_key_is_sent_as_a_bearer_header(agent_mod):
    seen = {}

    def _open(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        return _Resp(200)

    agent_mod.llm_preflight("https://api.deepseek.com", "sk-secret",
                            resolve=_resolve_ok, open_url=_open)
    assert seen["auth"] == "Bearer sk-secret"


# ------------------------------------------------- describe_llm_error
class _ApiError(Exception):
    def __init__(self, message="boom", status_code=None):
        super().__init__(message)
        self.status_code = status_code


# names mirror the openai SDK so name-based classification is exercised
class APIConnectionError(_ApiError): pass


class AuthenticationError(_ApiError): pass


class PermissionDeniedError(_ApiError): pass


class NotFoundError(_ApiError): pass


class RateLimitError(_ApiError): pass


class BadRequestError(_ApiError): pass


class InternalServerError(_ApiError): pass


BASE = "https://api.deepseek.com"
MODEL = "deepseek-v4-flash"


def _describe(agent_mod, exc, **kw):
    return agent_mod.describe_llm_error(exc, BASE, MODEL, "deepseek", **kw)


def test_connection_error_explains_the_sdk_conflates_transport_failures(agent_mod):
    out = _describe(agent_mod, APIConnectionError("Connection error."))
    assert "Network/transport failure" in out
    assert "grep -iE 'proxy'" in out, "must suggest checking proxy vars"
    assert "curl -sS" in out and BASE in out, "must give a copy-pasteable probe"


def test_connection_error_is_recognized_by_status_too(agent_mod):
    """A generic exception carrying a status code must still classify."""
    out = _describe(agent_mod, _ApiError("gateway blew up", status_code=502))
    assert "server-side" in out and "502" in out


@pytest.mark.parametrize("exc,fragment", [
    (AuthenticationError("bad key", status_code=401), "API key"),
    (AuthenticationError("Incorrect API key provided"), "API key"),
    (PermissionDeniedError("nope", status_code=403), "not allowed"),
    (NotFoundError("no model", status_code=404), "Model in use"),
    (RateLimitError("slow down", status_code=429), "Rate limited"),
    (BadRequestError("too long", status_code=400), "context length"),
])
def test_status_specific_guidance(agent_mod, exc, fragment):
    assert fragment in _describe(agent_mod, exc)


def test_404_by_name_without_status_still_finds_the_model_hint(agent_mod):
    out = _describe(agent_mod, NotFoundError("model does not exist"))
    assert "Model in use: " + MODEL in out


def test_unknown_failure_points_at_the_debug_flag(agent_mod):
    out = _describe(agent_mod, ValueError("something odd"))
    assert "AGENT_DEBUG=1" in out


def test_help_block_always_names_endpoint_and_model(agent_mod):
    out = _describe(agent_mod, RateLimitError("x", status_code=429))
    assert f"endpoint : {BASE}" in out
    assert f"model    : {MODEL}" in out
    assert "provider : deepseek" in out


def test_help_block_offers_the_offline_path(agent_mod):
    out = _describe(agent_mod, AuthenticationError("x", status_code=401))
    assert "DOCTOR=1 scripts/run-all-agentic.sh" in out


def test_model_defaults_when_not_supplied(agent_mod):
    out = agent_mod.describe_llm_error(RateLimitError("x", status_code=429), BASE)
    assert "(provider default)" in out


# ------------------------------------------------------------ _is_llm_error
def test_is_llm_error_for_sdk_and_transport_types(agent_mod):
    assert agent_mod._is_llm_error(APIConnectionError("x")) is True
    assert agent_mod._is_llm_error(RateLimitError("x")) is True
    assert agent_mod._is_llm_error(TimeoutError("x")) is True


def test_is_llm_error_rejects_our_own_bugs(agent_mod):
    """A KeyError in our own code must NOT be dressed up as a network problem."""
    assert agent_mod._is_llm_error(ValueError("bug")) is False
    assert agent_mod._is_llm_error(KeyError("bug")) is False


def test_is_llm_error_detects_the_real_sdk_module(agent_mod):
    httpx = pytest.importorskip("httpx")
    openai = pytest.importorskip("openai")
    exc = openai.APIConnectionError(request=httpx.Request("POST", BASE))
    assert agent_mod._is_llm_error(exc) is True
    assert "Network/transport failure" in _describe(agent_mod, exc)
