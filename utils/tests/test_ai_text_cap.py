"""The completion ceiling sent to OpenAI leaves room for a reasoning model."""

from unittest.mock import patch

from django.test import override_settings

from utils import ai_text


def _reply(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


@override_settings(OPENAI_API_KEY="test-key", OPENAI_MODEL="gpt-6.1-terra")
def test_small_caps_are_raised_to_the_reasoning_floor():
    with patch("utils.ai_text._post_json", return_value=_reply("ok")) as post:
        assert ai_text.generate_summary("system", "user", max_tokens=400) == "ok"
    payload = post.call_args.args[1]
    assert payload["model"] == "gpt-6.1-terra"
    assert payload["max_completion_tokens"] == ai_text._MIN_COMPLETION_TOKENS


@override_settings(OPENAI_API_KEY="test-key", OPENAI_MODEL="gpt-6.1-terra")
def test_larger_caps_are_left_alone():
    with patch("utils.ai_text._post_json", return_value=_reply("ok")) as post:
        ai_text.generate_summary("system", "user", max_tokens=8000)
    assert post.call_args.args[1]["max_completion_tokens"] == 8000


@override_settings(
    OPENAI_API_KEY="test-key", OPENAI_MODEL="gpt-6.1-terra", OPENAI_MODEL_SMALL="gpt-5-mini"
)
def test_sorting_jobs_use_the_small_model_and_writing_uses_the_main_one():
    with patch("utils.ai_text._post_json", return_value=_reply('{"a": 1}')) as post:
        ai_text.generate_json("s", "u", schema={"type": "object"}, small=True)
        ai_text.generate_json("s", "u", schema={"type": "object"})
    assert [c.args[1]["model"] for c in post.call_args_list] == ["gpt-5-mini", "gpt-6.1-terra"]


@override_settings(OPENAI_API_KEY="test-key", OPENAI_MODEL="gpt-6.1-terra", OPENAI_MODEL_SMALL="")
def test_no_small_model_set_means_everything_uses_the_main_one():
    with patch("utils.ai_text._post_json", return_value=_reply('{"a": 1}')) as post:
        ai_text.generate_json("s", "u", schema={"type": "object"}, small=True)
    assert post.call_args.args[1]["model"] == "gpt-6.1-terra"
