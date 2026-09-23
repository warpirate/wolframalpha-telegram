import ranking


def _pyq(chapter_id, title, exam, n, subject="History"):
    return {"subject": subject, "chapter_id": chapter_id, "title": title, "exam": exam, "n": n}


def test_build_units_merges_by_chapter():
    units = ranking.build_units(
        [_pyq(1, "Mauryan Age", "SI", 5), _pyq(1, "Mauryan Age", "PC", 2), _pyq(None, "Preamble", "", 3, "Polity")],
        {"pages": [{"subject": "History", "chapter_id": 1, "title": "Mauryan Age", "pages": 4}],
         "attempts": [{"subject": "History", "chapter_id": 1, "title": "Mauryan Age",
                       "attempts": 10, "correct": 4}]},
    )
    by_title = {u.title: u for u in units}
    mauryan = by_title["Mauryan Age"]
    assert (mauryan.si, mauryan.pc, mauryan.pages, mauryan.attempts, mauryan.correct) == (5, 2, 4, 10, 4)
    assert by_title["Preamble"].other == 3


def test_untouched_high_weight_beats_mastered():
    units = ranking.build_units(
        [_pyq(1, "Mastered", "SI", 10), _pyq(2, "Untouched", "SI", 8)],
        {"pages": [{"subject": "History", "chapter_id": 1, "title": "Mastered", "pages": 3}],
         "attempts": [{"subject": "History", "chapter_id": 1, "title": "Mastered",
                       "attempts": 10, "correct": 10}]},
    )
    ranked = ranking.score_units(units)
    assert ranked[0].title == "Untouched"


def test_weak_topic_is_boosted():
    units = ranking.build_units(
        [_pyq(1, "Weak", "SI", 5), _pyq(2, "Fresh", "SI", 5)],
        {"pages": [], "attempts": [{"subject": "History", "chapter_id": 1, "title": "Weak",
                                    "attempts": 4, "correct": 1}]},
    )
    assert ranking.score_units(units)[0].title == "Weak"


def test_plan_uses_estimates_when_few_pyqs():
    text = ranking.render_plan(ranking.build_units([_pyq(1, "Mauryan Age", "SI", 3)], {}))
    assert "estimate" in text.lower()
    assert "Reasoning" in text and "no book" in text


def test_plan_ranks_when_enough_pyqs():
    rows = [_pyq(i, f"Chapter {i}", "SI", 10 - i) for i in range(1, 6)]
    text = ranking.render_plan(ranking.build_units(rows, {}))
    assert text.index("Chapter 1") < text.index("Chapter 2")
    assert "No book yet for" in text
