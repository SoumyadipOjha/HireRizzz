"""CLI smoke tests (would have caught a broken cli.py that no other test imports)."""

from __future__ import annotations

import pytest

from screening import cli


def test_every_command_parses():
    p = cli.build_parser()
    for argv in (["ingest"], ["extract", "--force"], ["shortlist", "--only", "x"], ["call"], ["run"], ["status"],
                 ["serve", "--port", "9999", "--host", "0.0.0.0", "--no-browser"], ["dashboard"],
                 ["simulate-call", "cid", "--script", "f.txt"], ["parse-transcript", "cid", "t.txt"],
                 ["check-llm"], ["export-schemas"]):
        assert p.parse_args(argv).func


def test_status_and_call_on_empty_data(tmp_path, capsys):
    assert cli.main(["--data-dir", str(tmp_path / "d"), "status"]) == 0
    assert "No candidates" in capsys.readouterr().out
    assert cli.main(["--data-dir", str(tmp_path / "d"), "call"]) == 0


def test_missing_key_is_a_config_error(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("screening.config.load_dotenv", lambda *a, **k: None)
    assert cli.main(["--data-dir", str(tmp_path / "d"), "extract"]) == 2
    assert "GEMINI_API_KEY is not set" in capsys.readouterr().err


def test_simulate_call_with_script(ctx, data_dir, tmp_path, monkeypatch, capsys):
    from conftest import FakeLLM

    from screening.stage0_ingest import ingest
    from screening.stage1_extract import run_stage1
    from screening.stage2_shortlist import run_stage2

    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    run_stage2(ctx, FakeLLM())
    aarav = next(e for e in ctx.index.all() if e.display_name == "Aarav Sharma")
    script = tmp_path / "answers.txt"
    n = len(ctx.config.questions.questions)
    script.write_text("\n".join(["yes"] + [f"answer {i}" for i in range(n)]), encoding="utf-8")
    monkeypatch.setattr(cli, "_llm", lambda ctx: FakeLLM())
    rc = cli.main(["--data-dir", str(data_dir), "simulate-call", aarav.candidate_id, "--script", str(script)])
    out = capsys.readouterr().out
    assert rc == 0 and "call ended: completed" in out and "Agent: Hi Aarav" in out


@pytest.mark.parametrize("cmd", ["serve", "dashboard"])
def test_serve_passes_host(monkeypatch, tmp_path, cmd):
    seen = {}
    monkeypatch.setattr("screening.dashboard.serve", lambda cfg, **kw: seen.update(kw))
    assert cli.main(["--data-dir", str(tmp_path), cmd, "--host", "0.0.0.0", "--no-browser"]) == 0
    assert seen == {"port": 8765, "open_browser": False, "host": "0.0.0.0"}
