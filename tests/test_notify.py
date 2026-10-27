from varbot import notify


def test_chunk_splits_on_newlines():
    text = "\n".join(f"line {i} " + "x" * 100 for i in range(100))
    parts = notify.chunk(text, size=1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == text.replace("\n", "")


def test_max_messages_truncates(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "http", lambda: type("H", (), {"post_json": staticmethod(lambda url, payload, **k: sent.append(payload))})())
    text = "a" * 10_000
    n = notify.send_telegram(text, chat_id="1", token="t", max_messages=2)
    assert n == 2 and len(sent) == 2
    assert sent[-1]["text"].endswith("full report in the reports directory)")
    assert all(len(p["text"]) <= notify.MAX_LEN for p in sent)
