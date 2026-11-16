from parity.timeline import (
    ANSWERED_EARLY,
    GARBLED_RESULT,
    MISHEARD,
    MISSED_HANDOFF,
    THINKER_WRONG,
    UNSCORED,
    analyse,
    answer_start_ms,
    heard_share,
    split_sentences,
)

SCRIPT = "I have a flute, a piano, a trombone and four stoves. How many musical instruments do I have?\nAnswer the question."
HEARD = [[500, 6000, "I have a flute, a piano, a trombone and four stoves. How many musical instruments do I have? Answer the question."]]


def run(output, delegations=(), heard=HEARD, question_end_ms=6200.0):
    return {
        "question_end_ms": question_end_ms,
        "input_segments": heard,
        "output_segments": output,
        "delegations": list(delegations),
        "stop_reason": "quiet",
        "usage_seconds": 20.0,
    }


def handoff(text, offset_ms=6500, done_ms=11000.0):
    return {"id": "item_1", "offset_ms": offset_ms, "done_ms": done_ms, "text": text, "status": "response.completed"}


def test_sentences_carry_the_start_time_of_their_first_fragment():
    segments = [[7000, 7400, "Let me "], [7400, 8000, "check that. "], [12000, 12300, "You have "], [12300, 12600, "three."]]
    sentences = split_sentences(segments)
    assert [(s.start_ms, s.text) for s in sentences] == [(7000.0, "Let me check that."), (12000.0, "You have three.")]


def test_answer_is_timed_from_the_sentence_that_carries_it_not_from_filler():
    segments = [[7000, 8000, "Let me check that. "], [12000, 12600, "You have three."]]
    assert answer_start_ms("object_counting", segments) == 12000.0


def test_a_corrected_answer_is_timed_from_the_correction():
    segments = [[7000, 7600, "You have four. "], [9000, 9800, "Actually, that's three."]]
    assert answer_start_ms("object_counting", segments) == 9000.0


def test_correct_run_has_no_cause_and_reports_time_to_answer():
    result = analyse("object_counting", "3", SCRIPT, run([[7000, 7600, "You have three."]]))
    assert result["correct"] is True and result["cause"] is None
    assert result["time_to_answer_s"] == 0.8
    assert result["delegated"] is False


def test_wrong_answer_without_a_handoff_is_a_missed_handoff():
    result = analyse("object_counting", "3", SCRIPT, run([[7000, 7600, "You have seven."]]))
    assert result["correct"] is False and result["cause"] == MISSED_HANDOFF


def test_wrong_backend_result_is_the_thinker_not_coordination():
    result = analyse(
        "object_counting", "3", SCRIPT,
        run([[11500, 12000, "You have seven."]], [handoff("The answer is seven.")]),
    )
    assert result["cause"] == THINKER_WRONG and result["backend_correct"] is False


def test_wrong_answer_spoken_before_the_result_arrived_is_answered_early():
    result = analyse(
        "object_counting", "3", SCRIPT,
        run([[7000, 7600, "You have seven."]], [handoff("You have three instruments.")]),
    )
    assert result["cause"] == ANSWERED_EARLY and result["backend_correct"] is True


def test_wrong_answer_spoken_after_a_right_result_is_garbled():
    result = analyse(
        "object_counting", "3", SCRIPT,
        run([[7000, 7800, "Let me check. "], [11500, 12000, "You have seven."]], [handoff("You have three instruments.")]),
    )
    assert result["cause"] == GARBLED_RESULT


def test_a_request_heard_wrongly_is_misheard_whatever_else_happened():
    garbled = [[500, 6000, "I have a fruit and four doves. How many do I have?"]]
    result = analyse("object_counting", "3", SCRIPT, run([[7000, 7600, "You have five."]], heard=garbled))
    assert result["cause"] == MISHEARD


def test_unreadable_reply_is_left_unscored():
    result = analyse("web_of_lies", "Yes", "Does Elanor tell the truth?", run([[7000, 7600, "Elanor tells the truth."]]))
    assert result["correct"] is None and result["cause"] == UNSCORED


def test_heard_share_ignores_small_words_and_punctuation():
    assert heard_share("How many musical instruments do I have?", "how many musical instruments do i have") == 1.0
    assert heard_share("a flute and a piano", "a flute") == 0.5
