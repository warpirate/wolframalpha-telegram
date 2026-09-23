import asyncio

import retrieval


def test_short_text_is_one_chunk():
    assert retrieval.chunk_text("  hello  ") == ["hello"]
    assert retrieval.chunk_text("   ") == []


def test_long_text_chunks_overlap_and_cover_everything():
    words = [f"w{i}" for i in range(600)]
    text = " ".join(words)
    chunks = retrieval.chunk_text(text, size=200, overlap=50)
    assert all(len(c) <= 200 for c in chunks)
    joined = " ".join(chunks)
    assert all(w in joined.split() for w in words)
    assert chunks[0].split()[-1] in chunks[1].split()  # overlap carries context across the cut


class FakeClient:
    def __init__(self):
        self.calls = []

    async def embed(self, texts, model, dimensions):
        self.calls.append(len(texts))
        return [[float(len(t)), 1.0] for t in texts]


def test_embed_batches(monkeypatch):
    monkeypatch.setattr(retrieval, "EMBED_BATCH", 2)
    client = FakeClient()
    vectors = asyncio.run(retrieval.embed(client, ["a", "bb", "ccc"]))
    assert client.calls == [2, 1] and vectors[2] == [3.0, 1.0]


def test_render_context_numbers_hits():
    text = retrieval.render_context([
        {"source": "pyq", "topic": "Mauryan Age", "text": "Q14 Who ..."},
        {"source": "page", "topic": "Dhamma", "text": "Ashoka's Dhamma ..."},
    ])
    assert text.startswith("[1] PYQ · Mauryan Age\nQ14 Who ...")
    assert "[2] Book page · Dhamma" in text
