import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import rules


def test_valid():
    for u in ["durov", "a_b_c", "user123", "abcde"]:
        ok, why = rules.validate(u)
        assert ok, (u, why)


def test_invalid():
    cases = {
        "ab": "короткий",
        "a" * 33: "длинный",
        "1abcde": "буквы",
        "_abcde": "буквы",
        "abcd_": "заканчивается",
        "ab__cd": "подчёркивани",
        "ab-cd": "только",
    }
    for u, word in cases.items():
        ok, why = rules.validate(u)
        assert not ok, u
        assert word in why, (u, why)


def test_normalize():
    assert rules.normalize("@Durov") == "durov"
    assert rules.normalize("https://t.me/Durov") == "durov"
    assert rules.normalize("telegram.me/durov") == "durov"
    assert rules.normalize("  durov ") == "durov"
