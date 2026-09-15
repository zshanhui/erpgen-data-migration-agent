"""Client error-message plumbing.

`_server_message` collapses a Frappe error body to its concise `exception`
message, so the agent-facing tool results stay readable instead of carrying a
multi-line traceback. It is also robust to the body being read truncated.
"""
from __future__ import annotations

import json

from erpgen.client import _server_message


def _body(exception: str | None = None, **extra) -> str:
    body: dict = {}
    if exception is not None:
        body["exception"] = exception
    body.setdefault("exc_type", "ValidationError")
    body.setdefault("exc", '["Traceback (most recent call last):\\n  ..."]')
    body.update(extra)
    return json.dumps(body)


def test_server_message_extracts_the_exception_field():
    exception = ('frappe.exceptions.ValidationError:  Supplier Type cannot be '
                 '"Hardware". It should be one of "Company", "Individual", '
                 '"Partnership"')
    assert _server_message(_body(exception=exception)) == exception


def test_server_message_survives_a_truncated_body():
    """The traceback field is cut off mid-JSON; json.loads would reject it."""
    truncated = '{"exception":"Supplier Type cannot be \\"Hardware\\"","exc_type":"Vali'
    assert _server_message(truncated) == 'Supplier Type cannot be "Hardware"'


def test_server_message_prefers_exception_over_message():
    assert _server_message('{"message":"m","exception":"e"}') == "e"


def test_server_message_falls_back_to_the_raw_detail():
    detail = '{"exc_type":"DoesNotExistError"}'
    assert _server_message(detail) == detail


def test_server_message_falls_back_to_plain_text():
    assert _server_message("MySQLdb.OperationalError: boom") == \
        "MySQLdb.OperationalError: boom"
