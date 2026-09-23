import json

import router


def test_index_page_parsed():
    raw = json.dumps({
        "kind": "index", "confidence": 0.9, "book": "karim", "subject": "history",
        "topic": "Contents", "page_no": None, "exam": "", "year": None, "text": "CHAPTER-6 ...",
        "chapters": [
            {"number": "6", "title": "The Mauryan Age", "page_start": 138, "page_end": "171",
             "topics": ["Ashoka's Dhamma", ""]},
            {"title": ""},
        ],
        "pyqs": [],
    })
    read = router.parse_page_read(raw)
    assert read.kind == "index" and read.book == "karim" and read.subject == "History"
    assert read.chapters == [{"number": 6, "title": "The Mauryan Age", "page_start": 138,
                              "page_end": 171, "topics": ["Ashoka's Dhamma"]}]


def test_pyq_inherits_page_exam_and_year():
    raw = json.dumps({
        "kind": "pyq", "confidence": 0.8, "book": "", "subject": "Polity", "exam": "si", "year": 2019,
        "pyqs": [{"number": 14, "question": "Who wrote the Preamble?", "options": ["a", "b", "c", "d"],
                  "answer": "b"}],
    })
    read = router.parse_page_read(raw)
    assert read.pyqs[0]["exam"] == "SI" and read.pyqs[0]["year"] == 2019
    assert read.pyqs[0]["number"] == 14


def test_bad_output_falls_back_to_other():
    read = router.parse_page_read("sorry, cannot help")
    assert read.kind == "other" and read.confidence == 0.0


def test_unknown_kind_and_book_are_cleaned():
    read = router.parse_page_read('{"kind": "poster", "book": "ncert", "confidence": 5}')
    assert read.kind == "other" and read.book == "" and read.confidence == 1.0


def test_index_without_chapters_is_low_confidence():
    read = router.parse_page_read('{"kind": "index", "confidence": 0.9, "chapters": []}')
    assert read.confidence <= 0.3


def test_quick_intent_question_numbers():
    for text, number in [("Q14", 14), ("help with q 7", 7), ("explain question 12", 12),
                         ("solve no. 3", 3), ("Q.21 please", 21)]:
        intent = router.quick_intent(text)
        assert intent is not None and intent.name == "lookup" and intent.number == number, text


def test_quick_intent_ignores_other_text():
    assert router.quick_intent("what should I study") is None
    assert router.quick_intent("15% of 640") is None


def test_parse_intent():
    intent = router.parse_intent('{"intent": "quiz", "subject": "polity"}', "quiz me on polity")
    assert intent.name == "quiz" and intent.subject == "Polity" and intent.query == "quiz me on polity"
    daily = router.parse_intent('{"intent": "daily", "hour": 30}', "daily at 30")
    assert daily.name == "daily" and daily.hour is None
    assert router.parse_intent("nonsense", "hi").name == "solve"
