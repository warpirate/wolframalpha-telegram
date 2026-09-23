import asyncio


def run(coro):
    return asyncio.run(coro)


def _page(**over):
    page = {"book_id": None, "chapter_id": None, "page_no": 150, "kind": "pyq",
            "subject": "History", "topic": "Mauryan Age", "text": "Q14 ...",
            "file_id": "F1", "file_unique_id": "U1"}
    page.update(over)
    return page


def _pyq(**over):
    item = {"book_id": None, "chapter_id": None, "page_id": None, "exam": "SI", "year": 2019,
            "number": 14, "question": "Who was Ashoka's father?", "options": ["a", "b", "c", "d"],
            "answer": "b", "subject": "History", "topic": "Mauryan Age"}
    item.update(over)
    return item


def test_book_upsert_is_idempotent(sqlite_db):
    first = run(sqlite_db.upsert_book(1, "karim", "Karim", "History"))
    again = run(sqlite_db.upsert_book(1, "karim", "Karim", "History"))
    assert first == again


def test_chapters_and_page_lookup(sqlite_db):
    book = run(sqlite_db.upsert_book(1, "karim", "Karim", "History"))
    cid, created = run(sqlite_db.upsert_chapter(1, book, {
        "number": 6, "title": "The Mauryan Age", "page_start": 138, "page_end": 171,
        "topics": ["Dhamma"]}, "b1"))
    assert created
    cid2, created2 = run(sqlite_db.upsert_chapter(1, book, {
        "number": 6, "title": "The Mauryan Age", "page_start": None, "page_end": None,
        "topics": []}, "b2"))
    assert cid2 == cid and not created2
    found = run(sqlite_db.find_chapter_for_page(1, book, 150))
    assert found["id"] == cid and found["topics"] == ["Dhamma"] and found["page_start"] == 138
    assert run(sqlite_db.find_chapter_for_page(1, book, 500)) is None


def test_page_duplicate_returns_none(sqlite_db):
    assert run(sqlite_db.add_page(1, _page(), "b1")) is not None
    assert run(sqlite_db.add_page(1, _page(), "b2")) is None


def test_pyq_duplicate_and_lookup_precedence(sqlite_db):
    page_a = run(sqlite_db.add_page(1, _page(), "b1"))
    page_b = run(sqlite_db.add_page(1, _page(file_unique_id="U2"), "b2"))
    first = run(sqlite_db.add_pyq(1, _pyq(page_id=page_a), "b1"))
    assert run(sqlite_db.add_pyq(1, _pyq(page_id=page_a), "b1")) is None
    second = run(sqlite_db.add_pyq(1, _pyq(page_id=page_b, question="Capital of Magadha?"), "b2"))
    assert run(sqlite_db.find_pyq(1, 14, page_id=page_a))["id"] == first
    latest = run(sqlite_db.find_pyq(1, 14))
    assert latest["id"] == second and latest["options"] == ["a", "b", "c", "d"]
    assert run(sqlite_db.find_pyq(1, 99)) is None


def test_vector_search_orders_by_similarity(sqlite_db):
    run(sqlite_db.add_chunks(1, "page", 1, [("ashoka", [1.0, 0.0]), ("gupta", [0.0, 1.0])],
                             "History", "t", "b1"))
    run(sqlite_db.add_chunks(2, "page", 9, [("other user", [1.0, 0.0])], "History", "t", "b9"))
    hits = run(sqlite_db.search_chunks(1, [0.9, 0.1], 5))
    assert [h["text"] for h in hits] == ["ashoka", "gupta"]
    assert hits[0]["score"] > 0.9
    assert run(sqlite_db.search_chunks(1, [1.0, 0.0], 5, ["pyq"])) == []


def test_undo_batch_removes_everything_from_that_batch(sqlite_db):
    page_id = run(sqlite_db.add_page(1, _page(), "b1"))
    run(sqlite_db.add_pyq(1, _pyq(page_id=page_id), "b1"))
    run(sqlite_db.add_chunks(1, "pyq", 1, [("x", [1.0])], "History", "t", "b1"))
    run(sqlite_db.add_questions(1, [{"question": "q?", "options": ["a", "b", "c", "d"],
                                     "correct_index": 0}], "src", batch_id="b1"))
    keep = run(sqlite_db.add_page(1, _page(file_unique_id="U9"), "b2"))
    assert run(sqlite_db.undo_batch(1, "b1")) == 4
    assert run(sqlite_db.get_page(1, page_id)) is None
    assert run(sqlite_db.get_page(1, keep)) is not None
    assert run(sqlite_db.undo_batch(1, "")) == 0


def test_counts_and_progress(sqlite_db):
    book = run(sqlite_db.upsert_book(1, "karim", "Karim", "History"))
    cid, _ = run(sqlite_db.upsert_chapter(1, book, {"number": 6, "title": "The Mauryan Age",
                                                     "page_start": 138, "page_end": 171,
                                                     "topics": []}, "b1"))
    run(sqlite_db.add_pyq(1, _pyq(chapter_id=cid), "b1"))
    run(sqlite_db.add_pyq(1, _pyq(chapter_id=cid, exam="PC", question="Other?"), "b1"))
    run(sqlite_db.add_page(1, _page(kind="content", chapter_id=cid), "b1"))
    counts = run(sqlite_db.pyq_counts(1))
    assert {(r["title"], r["exam"], r["n"]) for r in counts} == {
        ("The Mauryan Age", "SI", 1), ("The Mauryan Age", "PC", 1)}
    progress = run(sqlite_db.topic_progress(1))
    assert progress["pages"][0]["chapter_id"] == cid and progress["pages"][0]["pages"] == 1
    assert progress["attempts"] == []
