"""P0: LLM endpoint preflight.

The OpenAI SDK reports DNS failures, TLS verification errors, a dead proxy and a
refused connection *identically* — as a bare `APIConnectionError` with no clue
which. `run_agent` therefore probes the endpoint first and turns that into an
actionable message. Pure: the network calls are injected through the
`resolve` / `open_url` seams, so none of this touches the network.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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


def test_root_404_is_reachable_and_does_not_blame_api_base(agent_mod):
    """An API root routinely has no handler; DNS+TCP+TLS all succeeded, so
    telling the user to check --api-base is misleading noise."""
    ok, detail = agent_mod.llm_preflight("https://api.deepseek.com", "k",
                                         resolve=_resolve_ok,
                                         open_url=_open_status(404))
    assert ok is True
    assert "reachable" in detail and "404" in detail
    assert "--api-base" not in detail


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


# ------------------------------------------------------- doctype resolution
def _sheet(tmp_path, name, header, row):
    p = tmp_path / name
    p.write_text(f"{header}\n{row}\n", encoding="utf-8")
    return str(p)


def test_doctype_is_inferred_from_the_source_headers(agent_mod, tmp_path):
    src = _sheet(tmp_path, "items.csv", "Item Code,Item Name,Group,UoM,Rate",
                 "MFG-1,Bearing,Products,Nos,45.5")
    assert agent_mod.resolve_doctype(
        argparse.Namespace(source=src, doctype=None)) == ("Item", False)


def test_explicit_doctype_overrides_inference(agent_mod, tmp_path):
    src = _sheet(tmp_path, "items.csv", "Item Code,Item Name", "MFG-1,Bearing")
    assert agent_mod.resolve_doctype(
        argparse.Namespace(source=src, doctype="Customer")) == ("Customer", False)


def test_flat_party_sheet_resolves_to_its_flow(agent_mod, tmp_path):
    src = _sheet(tmp_path, "suppliers-smb.csv",
                 "Supplier Name,Supplier Type,Supplier Group,Contact Name,Email",
                 "Acme,Company,Raw Material,Alicia,alicia@acme.example")
    assert agent_mod.resolve_doctype(
        argparse.Namespace(source=src, doctype=None)) == ("suppliers_full", True)
    src2 = _sheet(tmp_path, "customers-smb.csv",
                  "Customer Name,Customer Type,Group,Contact Name,Email",
                  "Acme,Company,Commercial,Alicia,alicia@acme.example")
    assert agent_mod.resolve_doctype(
        argparse.Namespace(source=src2, doctype=None)) == ("customers_full", True)


def test_unrecognised_sheet_resolves_to_none_not_a_guess(agent_mod, tmp_path):
    src = _sheet(tmp_path, "mystery.csv", "Foo,Bar", "1,2")
    assert agent_mod.resolve_doctype(
        argparse.Namespace(source=src, doctype=None)) == (None, False)


def test_no_source_no_doctype_resolves_to_none(agent_mod):
    assert agent_mod.resolve_doctype(
        argparse.Namespace(source=None, doctype=None)) == (None, False)


def test_doctype_flag_has_no_argparse_default(agent_mod):
    """Regression: `default="Customer"` made `args.doctype or guess_doctype(...)`
    always short-circuit, so EVERY single-doctype sheet was mapped against
    Customer (Item columns analysed as customer_name/customer_type)."""
    args = agent_mod.build_parser().parse_args(["--source", "samples/items.csv"])
    assert args.doctype is None, "a default here silently disables inference"


def test_max_iterations_flag_is_exposed(agent_mod):
    args = agent_mod.build_parser().parse_args([])
    assert args.max_iterations >= 20, "llama-index's own default of 20 is too low"


# ----------------------------------------------------- iteration exhaustion
class WorkflowRuntimeError(Exception): pass


def test_iteration_exhaustion_is_recognized_by_name(agent_mod):
    assert agent_mod._is_iteration_exhausted(WorkflowRuntimeError("boom")) is True


def test_iteration_exhaustion_is_recognized_by_message(agent_mod):
    assert agent_mod._is_iteration_exhausted(
        RuntimeError("Max iterations of 20 reached!")) is True


def test_iteration_exhaustion_ignores_unrelated_errors(agent_mod):
    assert agent_mod._is_iteration_exhausted(ValueError("nope")) is False


def test_iteration_exhaustion_is_not_mistaken_for_a_network_error(agent_mod):
    """It must not be swallowed by the LLM help block."""
    assert agent_mod._is_llm_error(WorkflowRuntimeError("Max iterations of 20 reached!")) is False


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
