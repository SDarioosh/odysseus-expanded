"""Fork: a failed approved web read continues the turn (src/web_read_recovery.py)."""
from pathlib import Path

from src.web_read_recovery import RECOVERY_NOTE, recover_failed_web_read

ROOT = Path(__file__).resolve().parents[1]
RATE_LIMIT = {
    "error": "web_fetch: https://shop.example.com/products/e1-max: Rate limit hit "
             "for https://shop.example.com/products/e1-max (attempt 0)",
    "exit_code": 1,
    "untrusted_content": True,
}


def test_failed_fetch_continues_with_a_note():
    result, recovered = recover_failed_web_read("web_fetch", RATE_LIMIT)
    assert recovered
    assert result["error"].startswith(RATE_LIMIT["error"])
    assert RECOVERY_NOTE in result["error"]
    assert result["exit_code"] == 1  # still a failure for the evidence ledger
    assert "Rate limit" in RATE_LIMIT["error"] and RECOVERY_NOTE not in RATE_LIMIT["error"]


def test_other_web_reads_continue_too():
    for tool in ("web_search", "pdf_extract", "youtube_tool"):
        assert recover_failed_web_read(tool, {"error": "timed out", "exit_code": 1})[1]


def test_refusals_keep_upstreams_report():
    for refusal in (
        {"error": "BLOCKED", "blocked": True, "exit_code": 1},
        {"error": "needs approval", "approval_required": True},
        {"error": "outside authority", "policy": "external_untrusted_context"},
    ):
        result, recovered = recover_failed_web_read("web_fetch", refusal)
        assert not recovered and result is refusal


def test_writes_and_successes_are_untouched():
    write_failure = {"error": "disk full", "exit_code": 1}
    assert recover_failed_web_read("manage_notes", write_failure) == (write_failure, False)
    assert recover_failed_web_read("bash", write_failure) == (write_failure, False)
    ok = {"output": "page text", "exit_code": 0}
    assert recover_failed_web_read("web_fetch", ok) == (ok, False)
    assert recover_failed_web_read("web_fetch", "text") == ("text", False)


def test_agent_loop_hook_skips_the_terminal_report():
    source = (ROOT / "src" / "agent_loop.py").read_text()
    hook = "approved_result, _fork_web_read_recovered = recover_failed_web_read("
    assert hook in source
    assert "elif not _fork_web_read_recovered and not tool_result_is_successful(approved_result)" in source
    assert source.index(hook) < source.index("The approved {approved.tool_name} action could not run")
