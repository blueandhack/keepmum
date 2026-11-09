"""The e2e cost report reads what the provider billed from the replies the tap recorded (e2e/cost.py)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from e2e import cost  # noqa: E402
from e2e.servers import Exchange  # noqa: E402


def sse(*events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def test_an_anthropic_stream_is_priced_from_its_start_and_its_last_delta():
    reply = sse({"type": "message_start", "message": {"model": "claude-haiku-5-5", "usage": {
                    "input_tokens": 10, "cache_read_input_tokens": 20_000, "cache_creation_input_tokens": 4_000,
                    "output_tokens": 1}}},
                {"type": "message_delta", "usage": {"output_tokens": 300}})
    u = cost.of([Exchange("anthropic", "POST", "/v1/messages", "", {}, b"", 200, reply)])
    assert (u.calls, u.input, u.cached, u.cache_write, u.output) == (1, 10, 20_000, 4_000, 300)
    assert round(u.cost, 7) == round((10 * 0.10 + 20_000 * 0.01 + 4_000 * 0.125 + 300 * 0.50) / 1e6, 7)


def test_a_responses_reply_counts_cached_input_apart_and_an_unknown_model_is_flagged():
    done = {"type": "response.completed", "response": {"model": "gpt-6-luna", "usage": {
        "input_tokens": 1_000, "input_tokens_details": {"cached_tokens": 800}, "output_tokens": 50}}}
    other = {"object": "response", "model": "gpt-9-new", "usage": {"input_tokens": 5, "output_tokens": 5}}
    u = cost.of([Exchange("openai", "POST", "/v1/responses", "", {}, b"", 200, sse(done)),
                 Exchange("chatgpt", "POST", "/backend-api/codex/responses", "", {}, b"", 200, json.dumps(other).encode()),
                 Exchange("openai", "POST", "/v1/responses", "", {}, b"", 429, sse(done))])  # refused: not billed
    assert (u.calls, u.input, u.cached, u.output) == (2, 205, 800, 55)
    assert u.unpriced == {"gpt-9-new"} and cost.dollars(u).endswith("+")
