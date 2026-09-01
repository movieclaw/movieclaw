"""OpenAI 兼容 mock LLM：首轮令 Agent 执行挂起的 bash，续轮给终答。"""
from __future__ import annotations

import json

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

MARKER = "movieclaw-chaos-marker"


def sse(obj) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/v1/chat/completions")
async def completions(req: Request):
    body = await req.json()
    messages = body.get("messages", [])
    model = body.get("model", "mock-model")
    if not body.get("stream"):
        # 供应商保存后的最小连通性验证走非流式
        return JSONResponse(
            {
                "id": "m1",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )
    has_tool = any(m.get("role") == "tool" for m in messages)
    base = {"id": "m1", "object": "chat.completion.chunk", "created": 0, "model": model}

    async def gen():
        if has_tool:
            yield sse(
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "已确认状态，任务继续。"},
                            "finish_reason": None,
                        }
                    ],
                }
            )
            yield sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        else:
            yield sse(
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_chaos",
                                        "type": "function",
                                        "function": {"name": "bash", "arguments": ""},
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ],
                }
            )
            args = json.dumps({"command": f"sleep 987654 # {MARKER}"})
            yield sse(
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [{"index": 0, "function": {"arguments": args}}]
                            },
                            "finish_reason": None,
                        }
                    ],
                }
            )
            yield sse(
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
            )
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=18802, log_level="warning")
