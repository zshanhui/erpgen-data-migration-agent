"""Live progress and call instrumentation for an agent run.

Two jobs, both about *watching* a run rather than deciding anything:

* the live progress stream on stdout (`--quiet` silences it) — a single round
  can take minutes and make dozens of remote calls, and without this the CLI
  user watches a blank screen;
* `instrumented_llm`, which logs every remote LLM call into the run transcript.

`instrumented_llm` exists because llama-index does **not** emit callback events
for `OpenAILike` (the `@llm_chat_callback()` decorator is only applied in
`custom.py` and `structured_llm.py`), so it wraps the two entry points
`FunctionAgent` actually funnels through by hand.

`_TRANSCRIPT_CTX` lives here because it is this layer's state: the tracer writes
it, and the run loop and the tools read it to publish the round number and the
`--run` id.
"""
from __future__ import annotations

import json
import time
from typing import Any

#: shared run state: {round, log_event, run, llm_calls}. The round loop sets it
#: before each round; the tracer and the run-id-aware tools read it.
_TRANSCRIPT_CTX: dict = {}

# ---------------------------------------------------------------- llm
# Live progress, streamed to stdout. A single round can take minutes and make
# dozens of remote calls; without this the CLI user watches a blank screen and
# has to guess whether it is working. `--quiet` silences it.
_LIVE: dict = {"enabled": True, "indent": "  ", "echo_content": True}


def _live(message: str) -> None:
    if _LIVE["enabled"]:
        print(message, flush=True)


def _brief(value: Any, limit: int = 100) -> str:
    """One-line, length-capped rendering for live output."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _brief_args(args, kwargs) -> str:
    if kwargs:
        parts = [f"{k}={_brief(v, 45)}" for k, v in kwargs.items()]
    else:
        parts = [_brief(a, 45) for a in args]
    return ", ".join(parts)


def _thinking(response) -> str:
    """The model's own words for this step — its visible reasoning."""
    msg = getattr(response, "message", None)
    return _brief(getattr(msg, "content", "") or "", 170)


def _requested_tools(response) -> list:
    """Names of the tools the model asked for in this response.

    llama-index exposes these on the response, which delegates to the message;
    fall back to the message directly for response shapes that only set it there.
    """
    calls = getattr(response, "tool_calls", None)
    if not calls:
        calls = getattr(getattr(response, "message", None), "tool_calls", None)
    names: list = []
    for call in (calls or []):
        name = getattr(call, "tool_name", None)
        if not name:
            tool = getattr(call, "tool", None)
            name = (getattr(getattr(tool, "metadata", None), "name", None)
                    or getattr(tool, "name", None))
        if name:
            names.append(str(name))
    return names


def _live_llm_request(messages) -> None:
    if _LIVE["enabled"]:
        _live(f"{_LIVE['indent']}· llm #{_TRANSCRIPT_CTX.get('llm_calls', 0)} → "
              f"{len(messages)} msg(s)")


def _live_llm_response(response, started: float) -> None:
    """Show what one remote call cost, and what it decided to do next."""
    if not _LIVE["enabled"]:
        return
    bits = [f"{_elapsed_ms(started)}ms"]
    usage = llm_usage(response)
    if usage.get("total_tokens") is not None:
        bits.append(f"{usage['total_tokens']:,} tok")
    wanted = _requested_tools(response)
    if wanted:
        bits.append("wants " + ", ".join(wanted))
    _live(f"{_LIVE['indent']}· llm #{_TRANSCRIPT_CTX.get('llm_calls', 0)} ← "
          + " · ".join(bits))
    if _LIVE.get("echo_content", True):
        thought = _thinking(response)
        if thought:
            _live(f"{_LIVE['indent']}    “{thought}”")


def _log_llm_event(event: str, **fields) -> None:
    """Record an LLM transport event on the active transcript (if any).

    Also counts requests, so the run can report how many remote calls it cost.
    """
    if event == "llm_request":
        _TRANSCRIPT_CTX["llm_calls"] = _TRANSCRIPT_CTX.get("llm_calls", 0) + 1
        fields.setdefault("call_no", _TRANSCRIPT_CTX["llm_calls"])
    log = _TRANSCRIPT_CTX.get("log_event")
    if log:
        log(event=event, round=_TRANSCRIPT_CTX.get("round"), **fields)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def llm_usage(response) -> dict:
    """Token usage from a ChatResponse, when the provider reports it.

    Deliberately tolerant: `raw` may be an OpenAI object, a plain dict, or
    absent entirely, and a missing usage block must never break a run.
    """
    raw = getattr(response, "raw", None)
    usage = getattr(raw, "usage", None)
    if usage is None and isinstance(raw, dict):
        usage = raw.get("usage")
    if usage is None:
        return {}

    def _get(key):
        if isinstance(usage, dict):
            return usage.get(key)
        return getattr(usage, key, None)

    out = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = _get(key)
        if value is not None:
            out[key] = value
    return out


def instrumented_llm(base_cls):
    """Wrap an LLM class so every remote call lands in the run transcript.

    llama-index does NOT emit callback events for `OpenAILike` (the
    `@llm_chat_callback()` decorator is only applied in `custom.py` and
    `structured_llm.py`), so this instruments the two entry points
    `FunctionAgent` actually funnels through: `achat_with_tools` -> `achat`,
    and `astream_chat_with_tools` -> `astream_chat`.

    Emits `llm_request` / `llm_response` / `llm_failure` with the round, elapsed
    time and token usage. Message *content* is never logged — only counts.
    """

    class _Instrumented(base_cls):
        async def achat(self, *args, **kwargs):
            messages = kwargs.get("messages") or (args[0] if args else None) or []
            started = time.monotonic()
            _log_llm_event(event="llm_request", method="achat",
                           model=str(getattr(self, "model", "") or ""),
                           messages=len(messages))
            _live_llm_request(messages)
            try:
                response = await super().achat(*args, **kwargs)
            except BaseException as e:
                _log_llm_event(event="llm_failure", method="achat",
                               duration_ms=_elapsed_ms(started),
                               error=f"{type(e).__name__}: {e}")
                _live(f"{_LIVE['indent']}· llm failed after "
                      f"{_elapsed_ms(started)}ms: {type(e).__name__}")
                raise
            _log_llm_event(event="llm_response", method="achat",
                           duration_ms=_elapsed_ms(started),
                           **llm_usage(response))
            _live_llm_response(response, started)
            return response

        async def astream_chat(self, *args, **kwargs):
            messages = kwargs.get("messages") or (args[0] if args else None) or []
            started = time.monotonic()
            _log_llm_event(event="llm_request", method="astream_chat",
                           model=str(getattr(self, "model", "") or ""),
                           messages=len(messages))
            _live_llm_request(messages)
            try:
                inner = await super().astream_chat(*args, **kwargs)
            except BaseException as e:
                # fails before a generator exists — still a remote call attempt
                _log_llm_event(event="llm_failure", method="astream_chat",
                               duration_ms=_elapsed_ms(started),
                               error=f"{type(e).__name__}: {e}")
                _live(f"{_LIVE['indent']}· llm failed after "
                      f"{_elapsed_ms(started)}ms: {type(e).__name__}")
                raise

            async def _logged():
                last = None
                try:
                    async for chunk in inner:
                        last = chunk
                        yield chunk
                except BaseException as e:
                    _log_llm_event(event="llm_failure", method="astream_chat",
                                   duration_ms=_elapsed_ms(started),
                                   error=f"{type(e).__name__}: {e}")
                    _live(f"{_LIVE['indent']}· llm stream failed after "
                          f"{_elapsed_ms(started)}ms: {type(e).__name__}")
                    raise
                # a streaming call is only complete once fully drained
                _log_llm_event(event="llm_response", method="astream_chat",
                               duration_ms=_elapsed_ms(started),
                               streamed=True, **llm_usage(last))
                _live_llm_response(last, started)

            return _logged()

    _Instrumented.__name__ = f"Instrumented{base_cls.__name__}"
    return _Instrumented


def _text(value) -> str:
    """Normalize an agent result to plain text.

    llama-index may hand back a str, a ChatMessage, or an AgentOutput depending
    on how the workflow finishes — unwrap any of them safely.

    A message with no content (the model ended its turn on a tool call) yields
    "", never a role repr like "user: None".
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    content = getattr(value, "content", None)
    if isinstance(content, str) and content.strip():
        return content
    # newer llama-index keeps text in .blocks
    blocks = getattr(value, "blocks", None) or []
    parts = [getattr(b, "text", None) for b in blocks if getattr(b, "text", None)]
    if parts:
        return "\n".join(parts)
    if content:
        return str(content)
    if hasattr(value, "role") or hasattr(value, "blocks"):
        return ""          # content-less chat message: no text to show
    return str(value)


def _safe(value, limit: int = 1000) -> str:
    """Serialize arbitrary tool args/results for the transcript log."""
    try:
        s = json.dumps(value, default=str)
    except Exception:
        s = str(value)
    return s[:limit]


def _wrap_tool(fn, name):
    """Wrap a tool function so every call is logged to the run transcript.

    functools.wraps preserves the original signature, so FunctionTool still
    builds the correct JSON schema for the model.
    """
    import functools

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        ctx = _TRANSCRIPT_CTX
        log = ctx.get("log_event")
        rnd = ctx.get("round")
        call_args = kwargs if kwargs else args
        if log:
            log(event="tool_call", round=rnd, name=name,
                kwargs=_safe(call_args))
        # greppable marker: `grep ToolUse:` lists every tool the agent ran
        _live(f"{_LIVE['indent']}  ToolUse:{name} {_brief_args(args, kwargs)}")
        try:
            result = fn(*args, **kwargs)
        except BaseException as e:
            # a raising tool must be visible immediately, not just in the log
            if log:
                log(event="tool_result", round=rnd, tool=name,
                    args=_safe(call_args), result_tail=f"raised {type(e).__name__}: {e}")
            _live(f"{_LIVE['indent']}    ToolResult:{name} ✗ "
                  f"{type(e).__name__}: {_brief(e, 90)}")
            raise
        if log:
            log(event="tool_result", round=rnd, tool=name,
                args=_safe(call_args),
                result_tail=_text(result)[-600:])
        text = _text(result)
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        _live(f"{_LIVE['indent']}    ToolResult:{name} ✓ {_brief(first, 100)}")
        return result

    return wrapped
