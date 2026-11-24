from utils.text_clip import clip_words


def test_short_text_is_unchanged():
    assert clip_words("Loved the mango flavor.", 40) == "Loved the mango flavor."


def test_cuts_on_a_word_boundary():
    text = "It wasn't really the best atmosphere for sampling today"
    # A fixed-width cut here read "...atmosphere for sa…"; whole words only.
    out = clip_words(text, 44)
    assert out == "It wasn't really the best atmosphere for…"
    assert len(out) <= 44


def test_never_ends_mid_word():
    text = "It wasn't really the best atmosphere for the demo"
    out = clip_words(text, 40)
    assert out.endswith("atmosphere…")


def test_drops_trailing_punctuation_before_the_ellipsis():
    assert clip_words("Busy store, lots of traffic, great day", 17) == "Busy store…"


def test_one_long_word_gets_a_hard_cut():
    out = clip_words("x" * 50, 10)
    assert out == "x" * 9 + "…"


def test_empty():
    assert clip_words(None, 10) == ""
    assert clip_words("", 10) == ""
