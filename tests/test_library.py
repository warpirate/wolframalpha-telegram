import asyncio
import json

import library


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    """complete_raw returns queued JSON replies; embed maps keywords to fixed vectors."""

    vision_model = "vision"
    KEYWORDS = ("maurya", "ashoka", "gupta", "preamble")

    def __init__(self, replies):
        self.replies = list(replies)

    async def complete_raw(self, messages, **kwargs):
        return self.replies.pop(0)

    async def embed(self, texts, model, dimensions):
        return [[1.0 if k in t.lower() else 0.0 for k in self.KEYWORDS] + [0.1] for t in texts]


INDEX = json.dumps({
    "kind": "index", "confidence": 0.9, "book": "karim", "subject": "History", "text": "contents",
    "chapters": [
        {"number": 6, "title": "The Mauryan Age", "page_start": 138, "page_end": 171,
         "topics": ["Ashoka's Dhamma"]},
        {"number": 8, "title": "The Age of Guptas", "page_start": 206, "page_end": 233, "topics": []},
    ],
})

PYQ = json.dumps({
    "kind": "pyq", "confidence": 0.9, "book": "karim", "subject": "History", "exam": "SI",
    "year": 2019, "text": "Q14 ...",
    "pyqs": [{"number": 14, "question": "Which Mauryan king issued the Dhamma edicts?",
              "options": ["Ashoka", "Bindusara", "Chandragupta", "Dasharatha"], "answer": "a"}],
})


def test_index_then_pyq_page(sqlite_db):
    client = FakeClient([INDEX, PYQ])
    saved = run(library.save_photo(client, 1, b"img", "F1", "U1", batch_id="b1"))
    assert saved.kind == "index" and "2 chapters" in saved.summary and not saved.needs_answer

    pyq = run(library.save_photo(client, 1, b"img", "F2", "U2", batch_id="b2"))
    assert pyq.kind == "pyq" and "1 PYQ" in pyq.summary
    stored = run(sqlite_db.find_pyq(1, 14))
    assert stored["exam"] == "SI" and stored["topic"] == "The Mauryan Age"  # mapped by similarity


def test_same_photo_twice_is_not_stored_again(sqlite_db):
    client = FakeClient([INDEX, INDEX])
    run(library.save_photo(client, 1, b"img", "F1", "U1", batch_id="b1"))
    again = run(library.save_photo(client, 1, b"img", "F1", "U1", batch_id="b2"))
    assert again.duplicate and again.page_id is None


def test_question_page_needs_answer(sqlite_db):
    client = FakeClient([json.dumps({"kind": "question", "confidence": 0.9, "text": "15% of 640?"})])
    saved = run(library.save_photo(client, 1, b"img", "F1", "U1", batch_id="b1"))
    assert saved.needs_answer and saved.summary == "" and saved.page_id is not None


def test_focus_expires():
    chat_data = {}
    library.set_focus(chat_data, topic="Mauryan Age", page_id=3)
    assert library.get_focus(chat_data)["page_id"] == 3
    chat_data[library.FOCUS_KEY]["until"] = 0
    assert library.get_focus(chat_data) == {}


def test_render_pyq():
    text = library.render_pyq({"number": 14, "exam": "SI", "year": 2019, "question": "Who?",
                               "options": ["A", "B"], "answer": "a"})
    assert text.splitlines()[0] == "SI 2019 · Q14"
    assert "(a) A" in text and "(b) B" in text
