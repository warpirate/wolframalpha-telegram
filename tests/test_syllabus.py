import syllabus


def test_book_lookup():
    assert syllabus.book_subject("karim") == "History"
    assert syllabus.book_title("lucent").startswith("General Science")
    assert syllabus.book_subject("nope") == "General"


def test_normalise_subject():
    assert syllabus.normalise_subject("polity") == "Polity"
    assert syllabus.normalise_subject(" current affairs ") == "Current Affairs"
    assert syllabus.normalise_subject("astrology") == "General"
    assert syllabus.normalise_subject(None) == "General"


def test_uncovered_subjects_excludes_the_four_books():
    gaps = syllabus.uncovered_subjects()
    assert "Reasoning" in gaps and "Telangana" in gaps
    assert not {"Polity", "History", "Arithmetic", "Science"} & set(gaps)


def test_estimates_sum_to_one():
    assert abs(sum(syllabus.ESTIMATED_SHARE.values()) - 1.0) < 1e-9
