"""P0: CLI argument plumbing and the pure argument helpers.

The `--log-dir` flag used to be registered only on the `import` subparser, so
`revert`/`status` crashed with "'Namespace' object has no attribute 'log_dir'".
These tests parse argv for *every* subcommand without dispatching a command, so
that class of bug fails here instead of after a real migration.
"""
from __future__ import annotations

import pytest

GLOBAL_FLAGS = ("base", "user", "password", "timeout", "log_dir", "run")

#: minimal argv that satisfies each subcommand's required arguments
MINIMAL_ARGV = {
    "map": ["samples/items.csv"],
    "import": ["samples/items.csv"],
    "createfield": ["Item"],
    "describe-doctype": ["Item"],
    "get-record": ["Item", "ITEM-1"],
    "list-records": ["Item"],
    "set-mapping": ["Item"],
    "revert": [],
    "create-record": ["UOM", "--fields", '{"uom_name": "Dozen"}'],
    "status": [],
    "delete": ["--doctype", "Item", "--names", "A"],
    "agent": ["--source", "samples/items.csv"],
}

EXPECTED_COMMANDS = set(MINIMAL_ARGV)


def parse(cli, argv):
    return cli.build_parser().parse_args(argv)


# ------------------------------------------------------------ command surface
def test_all_expected_subcommands_are_registered(cli):
    ap = cli.build_parser()
    choices = set(ap._subparsers._group_actions[0].choices)
    assert choices == EXPECTED_COMMANDS


@pytest.mark.parametrize("command", sorted(EXPECTED_COMMANDS))
def test_every_subcommand_parses_with_minimal_argv(cli, command):
    args = parse(cli, [command, *MINIMAL_ARGV[command]])
    assert args.cmd == command
    assert callable(args.fn)


@pytest.mark.parametrize("command", sorted(EXPECTED_COMMANDS))
def test_every_subcommand_carries_the_global_flags(cli, command):
    """Commands read args.log_dir / args.run directly, so they must always exist."""
    args = parse(cli, [command, *MINIMAL_ARGV[command]])
    for flag in GLOBAL_FLAGS:
        assert hasattr(args, flag), f"{command} is missing args.{flag}"


@pytest.mark.parametrize("command", sorted(EXPECTED_COMMANDS))
def test_log_dir_override_reaches_every_subcommand(cli, command):
    args = parse(cli, ["--log-dir", "/tmp/elsewhere", command, *MINIMAL_ARGV[command]])
    assert args.log_dir == "/tmp/elsewhere"


@pytest.mark.parametrize("command", sorted(EXPECTED_COMMANDS))
def test_run_id_reaches_every_subcommand(cli, command):
    args = parse(cli, ["--run", "acme-01", command, *MINIMAL_ARGV[command]])
    assert args.run == "acme-01"


def test_globals_default_to_the_demo_stack(cli):
    args = parse(cli, ["status"])
    assert args.base == "http://localhost:8082"
    assert args.user == "Administrator"
    assert args.log_dir == "logs"
    assert args.run is None
    assert args.timeout == 300


def test_revert_defaults_to_a_dry_run(cli):
    args = parse(cli, ["revert", "--latest", "Item"])
    assert args.apply is False
    assert args.force is False
    assert args.latest == "Item"
    assert args.log_dir == "logs"  # the regression this file exists for


def test_import_apply_is_opt_in(cli):
    args = parse(cli, ["import", "s.csv"])
    assert args.apply is False
    assert args.bulk is False
    assert args.bypass_conflicts is False


def test_status_accepts_a_run_id_or_latest(cli):
    assert parse(cli, ["status", "acme-01"]).run_log == "acme-01"
    assert parse(cli, ["status", "--latest", "Item"]).latest == "Item"


def test_missing_required_argument_exits_2(cli):
    with pytest.raises(SystemExit) as e:
        parse(cli, ["map"])
    assert e.value.code == 2


def test_unknown_subcommand_exits_2(cli):
    with pytest.raises(SystemExit) as e:
        parse(cli, ["frobnicate"])
    assert e.value.code == 2


def test_help_mentions_every_command(cli, capsys):
    with pytest.raises(SystemExit):
        parse(cli, ["--help"])
    out = capsys.readouterr().out
    for command in EXPECTED_COMMANDS:
        assert command in out


# --------------------------------------------------------- the agent entrypoint
def test_agent_subcommand_dispatches_to_the_in_package_agent(cli, monkeypatch):
    """`erpgen agent` is the one entrypoint; the old standalone script is gone."""
    import erpgen.agent

    seen = []
    monkeypatch.setattr(erpgen.agent, "run", lambda args: seen.append(args) or 7)
    args = parse(cli, ["--run", "acme-01", "agent", "--source", "samples/items.csv"])
    assert args.fn(args) == 7
    assert seen[0].source == "samples/items.csv"
    assert seen[0].run == "acme-01"


def test_the_standalone_agent_script_no_longer_exists(cli):
    from pathlib import Path
    assert not (Path(cli.__file__).parent / "scripts" / "agent.py").exists()


def test_agent_doctor_runs_through_the_cli_without_an_llm(cli, monkeypatch, capsys):
    from pathlib import Path
    monkeypatch.chdir(Path(cli.__file__).parent)
    monkeypatch.setattr("sys.argv", ["erpgen.py", "agent", "--doctor",
                                     "--source", "samples/items.csv"])
    assert cli.main() == 0
    assert "doctype=Item" in capsys.readouterr().out


# ------------------------------------------------------------ pure helpers
@pytest.mark.parametrize("label,expected", [
    ("Vendor Code", "vendor_code"),
    ("Customer Since", "customer_since"),
    ("Tax ID", "tax_id"),
    ("  Spaced  Out  ", "spaced_out"),
    ("Already_Snake", "already_snake"),
    ("A--B", "a_b"),
    ("Notes", "notes"),
])
def test_snake_derives_fieldnames(cli, label, expected):
    assert cli._snake(label) == expected


def test_json_arg_none_is_none(cli):
    assert cli._json_arg("fields", None) is None


def test_json_arg_parses_valid_json(cli):
    assert cli._json_arg("fields", '["name", "item_group"]') == ["name", "item_group"]
    assert cli._json_arg("filter", '{"item_group": "Products"}') == {"item_group": "Products"}


def test_json_arg_rejects_invalid_json_with_a_friendly_error(cli, capsys):
    with pytest.raises(SystemExit) as e:
        cli._json_arg("defaults", "{bad json}")
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "must be valid JSON" in err
    assert "defaults" in err


# ------------------------------------------------- _inject_id_column
def test_inject_id_column_writes_the_source_value(cli):
    from conftest import make_sheet
    sheet = make_sheet(["Customer Name", "Vendor Code"],
                       [["Acme", "VC-1"], ["Beta", "VC-2"]])
    payloads = [{"__row": 2}, {"__row": 3}]
    cli._inject_id_column(sheet, payloads, _plan(id_field="name"), "Vendor Code")
    assert [p["name"] for p in payloads] == ["VC-1", "VC-2"]


def test_inject_id_column_uses_the_doctype_id_field(cli):
    from conftest import make_sheet
    sheet = make_sheet(["Item Code"], [["ITEM-1"]])
    payloads = [{"__row": 2}]
    cli._inject_id_column(sheet, payloads, _plan(id_field="item_code"), "Item Code")
    assert payloads[0]["item_code"] == "ITEM-1"


def test_inject_id_column_ignores_an_unknown_column(cli):
    from conftest import make_sheet
    sheet = make_sheet(["A"], [["x"]])
    payloads = [{"__row": 2}]
    cli._inject_id_column(sheet, payloads, _plan(id_field="name"), "Missing")
    assert "name" not in payloads[0]


def test_inject_id_column_skips_blank_cells_and_out_of_range_rows(cli):
    from conftest import make_sheet
    sheet = make_sheet(["Code"], [[""], ["C-2"]])
    payloads = [{"__row": 2}, {"__row": 3}, {"__row": 99}, {}]
    cli._inject_id_column(sheet, payloads, _plan(id_field="name"), "Code")
    assert payloads[0] == {"__row": 2}
    assert payloads[1]["name"] == "C-2"
    assert "name" not in payloads[2]   # beyond the last row
    assert "name" not in payloads[3]   # no __row marker


def _plan(id_field: str):
    from erpgen.mapper import MappingPlan
    plan = MappingPlan(doctype="Customer")
    plan.id_field = id_field
    return plan
