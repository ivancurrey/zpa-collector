from collector import selftest


def test_run_selftest_returns_zero(capsys):
    rc = selftest.run_selftest()
    out = capsys.readouterr().out
    assert rc == 0, f"selftest did not pass; output:\n{out}"
    assert "PASS" in out
