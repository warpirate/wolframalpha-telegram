from formatter import (
    TELEGRAM_MAX_MESSAGE_LEN,
    escape_markdown_v2,
    latex_to_unicode,
    prepare_for_telegram,
    split_message,
    unescape_markdown_v2,
)


def test_latex_becomes_unicode():
    assert latex_to_unicode(r"\frac{-b \pm \sqrt{\Delta}}{2a}") == "(-b ± √(∆))/(2a)"
    assert latex_to_unicode(r"x^{2} + H_{2}O") == "x² + H₂O"
    assert latex_to_unicode(r"$$\int_0^\infty$$") == "∫₀^∞"
    assert latex_to_unicode(r"$x^2$ is $\theta$") == "x² is θ"


def test_money_is_not_math():
    assert latex_to_unicode("costs $5 and $10") == "costs $5 and $10"


def test_escape_round_trip():
    raw = "a_b*c[d](e)~`>#+-=|{}.!\\"
    escaped = escape_markdown_v2(raw)
    assert all(escaped[i - 1] == "\\" for i, ch in enumerate(escaped) if ch in "_*[]().!" and i)
    assert unescape_markdown_v2(escaped) == raw


def test_bold_is_kept_but_arithmetic_is_escaped():
    assert prepare_for_telegram("**Answer**: 2.5 (approx) - done!") == [
        r"*Answer*: 2\.5 \(approx\) \- done\!"
    ]
    assert prepare_for_telegram("2*3 = 6 and a*b") == [r"2\*3 \= 6 and a\*b"]
    assert prepare_for_telegram("✅ *Answer*\n96") == ["✅ *Answer*\n96"]


def test_split_respects_limit_and_keeps_text():
    text = ("word " * 300 + "\n") * 5
    chunks = split_message(text, 1000)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert " ".join(" ".join(chunks).split()) == " ".join(text.split())


def test_split_never_breaks_an_escape_pair():
    chunks = split_message("a" * 9 + "\\." + "b" * 5, 10)
    assert not any(chunk.endswith("\\") for chunk in chunks)


def test_split_is_capped_at_telegram_limit():
    chunks = split_message("x " * 5000, 10_000)
    assert all(len(chunk) <= TELEGRAM_MAX_MESSAGE_LEN for chunk in chunks)


def test_empty_input():
    assert prepare_for_telegram("   ") == []
    assert split_message("") == []
