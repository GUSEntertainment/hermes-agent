from agent.context_compressor import ContextCompressor, SUMMARY_PREFIX


def test_find_latest_context_summary_clamps_out_of_range_window() -> None:
    messages = [
        {"role": "user", "content": "plain user turn"},
        {"role": "assistant", "content": f"{SUMMARY_PREFIX}kept summary"},
    ]

    idx, summary = ContextCompressor._find_latest_context_summary(
        messages,
        start=-50,
        end=50,
    )

    assert idx == 1
    assert summary == "kept summary"


def test_find_latest_context_summary_handles_empty_out_of_range_window() -> None:
    idx, summary = ContextCompressor._find_latest_context_summary(
        [{"role": "user", "content": "plain user turn"}],
        start=10,
        end=50,
    )

    assert idx is None
    assert summary == ""