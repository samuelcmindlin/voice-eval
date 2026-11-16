import pytest

from parity.answers import FALLBACK, LEAD, PATTERN, extract, is_correct


@pytest.mark.parametrize(
    "reply, expected, method",
    [
        ("Invalid.", "invalid", LEAD),
        ("So, it's valid.", "valid", FALLBACK),
        ("Okay, valid. The conclusion follows from the premises.", "valid", LEAD),
        ("The argument is deductively invalid.", "invalid", FALLBACK),
        ("That argument is not valid, because the premise only goes one way.", "invalid", FALLBACK),
        ("It isn't valid.", "invalid", FALLBACK),
        ("The answer is invalid.", "invalid", PATTERN),
        ("Is it valid or invalid? Working through it, the conclusion does follow, so the answer is valid.", "valid", PATTERN),
        ("At first it looks valid, but the second premise does not support it, so it is invalid.", "invalid", FALLBACK),
        ("**Invalid** - the argument affirms the consequent.", "invalid", LEAD),
    ],
)
def test_validity(reply, expected, method):
    got = extract("formal_fallacies", reply)
    assert (got.answer, got.method) == (expected, method)


@pytest.mark.parametrize(
    "reply, expected, method",
    [
        ("Yes.", "Yes", LEAD),
        ("No, you end up three steps to the left.", "No", LEAD),
        ("Well, no. You finish one step behind where you started.", "No", LEAD),
        ("Yep, you're back where you began.", "Yes", LEAD),
        ("Let me work through it. Two forward, two back. So the answer is yes.", "Yes", PATTERN),
        ("After following the steps you are at the origin again. Yes.", "Yes", FALLBACK),
        ("There are no steps left over, so, yes, you return to the start.", "Yes", FALLBACK),
    ],
)
def test_yes_no(reply, expected, method):
    got = extract("navigate", reply)
    assert (got.answer, got.method) == (expected, method)


@pytest.mark.parametrize(
    "reply",
    [
        "You end up back at the starting point.",          # no explicit yes or no
        "Elanor tells the truth.",                         # needs reading, not matching
        "I'm checking that for you now.",
        "",
    ],
)
def test_yes_no_unclear_is_left_for_a_person(reply):
    assert not extract("web_of_lies", reply).found


@pytest.mark.parametrize(
    "reply, expected, method",
    [
        ("Eight.", "8", LEAD),
        ("You have 11 musical instruments.", "11", PATTERN),
        ("That's a flute, a piano and four stoves, so the answer is six.", "6", PATTERN),
        ("Let's count: 1 flute, 1 piano, 1 trombone. That makes three instruments.", "3", PATTERN),
        ("Counting them up gives twenty-one in total.", "21", PATTERN),
        ("There are seven. Want me to go through them one by one?", "7", PATTERN),
        ("I count 4 fruits, since the two stoves are not fruit.", "4", PATTERN),
        ("A flute, a piano, and then the trombone and the violin: four.", "4", FALLBACK),
    ],
)
def test_counts(reply, expected, method):
    got = extract("object_counting", reply)
    assert (got.answer, got.method) == (expected, method)


def test_scoring():
    assert is_correct("object_counting", "8", "8") is True
    assert is_correct("object_counting", "08", "8") is True
    assert is_correct("navigate", "Yes", "No") is False
    assert is_correct("formal_fallacies", "invalid", "invalid") is True
    assert is_correct("web_of_lies", None, "Yes") is None


def test_unknown_category():
    with pytest.raises(ValueError):
        extract("riddles", "Yes.")
