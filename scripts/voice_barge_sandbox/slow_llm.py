"""Stand-in for llama-server: an OpenAI-compatible model that takes DELAY seconds to answer
(the time a real model spends on tool calls and a long answer). Logs each request and
whether the caller hung up before the answer was done."""
import asyncio, json, os, sys, time
from aiohttp import web

DELAY = float(os.environ.get("SLOW_LLM_DELAY", "25"))

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

def last_user(body):
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return str(c)
    return ""

async def models(_):
    return web.json_response({"object": "list", "data": [{"id": "slow-model", "object": "model"}]})

async def chat(request):
    body = await request.json()
    q = last_user(body)[-80:].replace("\n", " ")
    rid = int(time.time() * 1000) % 100000
    log(f"[{rid}] request (stream={body.get('stream')}) ...{q!r}; answering in {DELAY:.0f} s")
    asked = q.split("question:")[-1].strip()
    answer = f"Here is the answer to: {asked}"
    t0 = time.time()
    if body.get("stream"):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        try:
            while time.time() - t0 < DELAY:
                await asyncio.sleep(0.5)
                if request.transport is None or request.transport.is_closing():
                    raise ConnectionResetError
            for w in answer.split(" "):
                d = {"id": "x", "object": "chat.completion.chunk", "model": "slow-model",
                     "choices": [{"index": 0, "delta": {"content": w + " "}, "finish_reason": None}]}
                await resp.write(f"data: {json.dumps(d)}\n\n".encode())
            d = {"id": "x", "object": "chat.completion.chunk", "model": "slow-model",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
            await resp.write(f"data: {json.dumps(d)}\n\ndata: [DONE]\n\n".encode())
            log(f"[{rid}] answered after {time.time()-t0:.1f} s")
        except (ConnectionResetError, asyncio.CancelledError):
            log(f"[{rid}] caller hung up after {time.time()-t0:.1f} s (model freed)")
            raise
        return resp
    try:
        await asyncio.sleep(DELAY)
    except asyncio.CancelledError:
        log(f"[{rid}] caller hung up after {time.time()-t0:.1f} s (model freed)")
        raise
    log(f"[{rid}] answered after {time.time()-t0:.1f} s")
    return web.json_response({"id": "x", "object": "chat.completion", "model": "slow-model",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": answer}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})

app = web.Application()
app.router.add_get("/v1/models", models)
app.router.add_post("/v1/chat/completions", chat)
web.run_app(app, host="127.0.0.1", port=8082, print=None)
