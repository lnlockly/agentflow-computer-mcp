from agentflow_computer_mcp.cli.format import fmt_budget, mask_key, table


def test_mask_key_short() -> None:
    assert mask_key("") == "(none)"
    assert mask_key("ab") == "ab..."


def test_mask_key_long() -> None:
    out = mask_key("af_live_1234567890abcdef")
    assert out == "af_live_..."
    assert "1234567890" not in out


def test_table_renders() -> None:
    rows = [{"id": "a", "name": "one"}, {"id": "bb", "name": "two"}]
    out = table(rows, ["id", "name"])
    assert "id" in out and "name" in out
    assert "one" in out and "two" in out


def test_table_empty() -> None:
    assert table([], ["id"]) == "(empty)"


def test_fmt_budget() -> None:
    assert fmt_budget(0.42, 5.0) == "$0.42 / $5.00 (8%)"
    assert fmt_budget(0.0, 0.0) == "$0.00 / $0.00 (0%)"
