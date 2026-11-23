import json
import httpx
from openai import OpenAI

from Backend.infrastructure.llm.openai_requester import OpenaiRequester


def test_structured_thinking_is_kept_out_of_translated_text(monkeypatch):
    content = [
        {"type": "thinking", "thinking": [{"type": "text", "text": "Internal reasoning"}]},
        {"type": "text", "text": '{"0":'},
        {"type": "text", "text": '"译文"}'},
    ]
    response = {
        "id": "probe", "created": 1, "model": "zai-glm-5-3-flash",
        "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": content,
        }}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    client = OpenAI(
        base_url="https://api.mistral.ai/v1", api_key="test-only",
        http_client=httpx.Client(transport=transport),
    )
    monkeypatch.setattr(
        "Backend.infrastructure.llm.openai_requester.LLMClientFactory.get_openai_client",
        lambda *_: client,
    )
    requester = OpenaiRequester()
    with client:
        skipped, thinking, answer, prompt, completion = requester.request_openai(
            [{"role": "user", "content": "Test"}], "",
            {"model_name": "zai-glm-5-3-flash", "api_url": "https://api.mistral.ai/v1"},
        )
    assert not skipped
    assert thinking == "Internal reasoning"
    assert json.loads(answer) == {"0": "译文"}
    assert (prompt, completion) == (12, 8)
    sse = "data: " + json.dumps({"choices": [{"delta": {"content": content}}]})
    assert requester._parse_sse_response(sse)[:2] == (thinking, answer)
