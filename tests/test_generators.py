import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import generators, rules


def test_parse_list():
    text = "@durov, t.me/abcde\nuser123; test_user"
    assert generators.parse_list(text) == ["durov", "abcde", "user123", "test_user"]


def test_parse_list_dedup_cap():
    assert generators.parse_list("aa aa bb bb cc") == ["aa", "bb", "cc"]
    big = " ".join(f"user{i}" for i in range(6000))
    assert len(generators.parse_list(big)) == 5000  # лимит из спеки


def test_pattern_sequential():
    cands = list(generators.pattern_candidates("ab?de", limit=50))
    assert len(cands) == 26
    assert cands[0] == "abade"
    assert all(rules.validate(c)[0] for c in cands)


def test_pattern_digits_and_alnum():
    cands = list(generators.pattern_candidates("t##ab", limit=1000))
    assert cands[0] == "t00ab" and len(cands) == 100
    cands2 = list(generators.pattern_candidates("a*bcd", limit=5000))
    assert len(cands2) == 36  # буква+цифра в середине
    assert all(len(c) == 5 for c in cands2)
    assert all(rules.validate(c)[0] for c in cands2)


def test_pattern_bad_char():
    assert list(generators.pattern_candidates("ka-a!")) == []


def test_random_styles():
    pr = list(generators.random_candidates(8, "pronounceable", 5))
    le = list(generators.random_candidates(8, "letters", 5))
    ld = list(generators.random_candidates(8, "letters_digits", 5))
    assert all(len(c) == 8 for c in pr + le + ld)
    assert all(rules.validate(c)[0] for c in pr + le + ld)
    assert len(set(pr)) == 5 and len(set(ld)) == 5


def test_dictionary():
    words = ["кот", "durov", "to", "valid_word", "OK_NAME"]
    got = list(generators.dictionary_candidates(words))
    assert got == ["durov", "valid_word", "ok_name"]


def test_auto_variants_order_and_dedup():
    from app.generators import auto_variants
    v = list(auto_variants("doston", 40))
    assert v[0] == "doston"                     # точный ник первым
    assert len(v) == len(set(v))                # без дубликатов
    assert all(len(u) >= 5 for u in v)          # валидные длины
    assert "doston1" in v[:5]                   # ближайшая мутация рано


def test_auto_variants_limit():
    from app.generators import auto_variants
    assert len(list(auto_variants("tester", 7))) == 7


def test_auto_variants_invalid_base():
    from app.generators import auto_variants
    assert list(auto_variants("", 10)) == []
