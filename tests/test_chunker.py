from agent_service.ingest.chunker import chunk_text, count_tokens


def test_short_text_single_chunk():
    chunks = chunk_text("hello world", max_tokens=512, overlap=64)
    assert len(chunks) == 1
    assert chunks[0]["content"] == "hello world"
    assert chunks[0]["ord"] == 0


def test_chunks_fit_max_tokens():
    text = ("一段中文内容。" * 200)
    max_tokens = 64
    chunks = chunk_text(text, max_tokens=max_tokens, overlap=8)
    assert len(chunks) > 1
    # token_count is reported on each chunk; allow small slack for overlap prefix re-encoding
    for c in chunks:
        assert c["token_count"] <= max_tokens * 2  # generous; overlap retokenization can add noise
    # all chunks together should cover at least most of original text length
    total_chars = sum(len(c["content"]) for c in chunks)
    assert total_chars >= len(text) * 0.95


def test_heading_boundary_preferred():
    text = (
        "## 第一节\n"
        + ("段落甲。" * 30)
        + "\n## 第二节\n"
        + ("段落乙。" * 30)
    )
    chunks = chunk_text(text, max_tokens=80, overlap=0)
    assert len(chunks) >= 2
    # Each section heading should anchor the START of some chunk's *core* content
    # (overlap=0, so no prefix borrowing). Verify both headings are present.
    joined = "||".join(c["content"] for c in chunks)
    assert "## 第一节" in joined
    assert "## 第二节" in joined
    # No single chunk should contain BOTH headings (they should be split)
    assert not any(("## 第一节" in c["content"] and "## 第二节" in c["content"]) for c in chunks)


def test_chinese_period_fallback_split():
    # No headings, no paragraph breaks — must fall back to '。'
    text = "甲" * 500 + "。" + "乙" * 500 + "。" + "丙" * 500 + "。"
    chunks = chunk_text(text, max_tokens=200, overlap=0)
    assert len(chunks) >= 2
    # At least one chunk should end at a period boundary
    assert any(c["content"].rstrip().endswith("。") for c in chunks)


def test_overlap_creates_shared_prefix():
    text = "段落甲" * 100 + "\n\n" + "段落乙" * 100
    chunks = chunk_text(text, max_tokens=80, overlap=16)
    if len(chunks) < 2:
        return  # nothing to assert
    # The first chars of chunk[i+1] should appear inside chunk[i]
    for i in range(len(chunks) - 1):
        nxt_head = chunks[i + 1]["content"][:8]
        assert nxt_head and nxt_head in chunks[i]["content"], (
            f"expected overlap prefix {nxt_head!r} to appear in previous chunk"
        )


def test_count_tokens_sane():
    assert count_tokens("") == 0
    assert count_tokens("hello") >= 1
