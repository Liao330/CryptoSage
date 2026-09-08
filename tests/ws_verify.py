"""通过真实 WS 端到端验证：消费事件直到 final，打印各节点 thinking 长度与最终研判。"""
import asyncio, json, sys
import websockets

TASK_ID = sys.argv[1]
URL = f"ws://127.0.0.1:8000/ws/{TASK_ID}"


async def main():
    final = None
    async with websockets.connect(URL, max_size=None) as ws:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=420)
            except asyncio.TimeoutError:
                print("!! 超时未收到 final")
                break
            ev = json.loads(raw)
            t = ev.get("type")
            d = ev.get("data") or {}
            if t == "ping":
                continue
            if t == "agent_start":
                if d.get("agent") == "orchestrator":
                    dec = d.get("decision") or {}
                    print(f"[orchestrator] next={dec.get('next_agents') or dec.get('next')} reason_len={len(dec.get('reasoning','') or '')}")
                else:
                    print(f"[start] {d.get('symbol','')} {d.get('query','')}")
            elif t == "signal":
                print(f"[signal] {d.get('agent'):11} bias={d.get('bias')} thinking_len={len(d.get('thinking','') or '')} tools={d.get('tools')}")
            elif t == "synthesis":
                print(f"[synthesis] direction={d.get('direction')} conf={d.get('confidence')} thinking_len={len(d.get('thinking','') or '')}")
            elif t == "critic":
                print(f"[critic] round={d.get('round')} valid={d.get('has_valid_critique')} thinking_len={len(d.get('thinking','') or '')}")
            elif t == "final":
                final = d
                break
    print("\n=== FINAL (HTTP+WS 端到端) ===")
    if final:
        print("direction :", final.get("direction"), "| confidence:", final.get("confidence"), "| market_state:", final.get("market_state"))
        print("error?    :", final.get("error"))
        print("key_findings:", json.dumps(final.get("key_findings"), ensure_ascii=False)[:400])
        print("position  :", json.dumps(final.get("position_advice"), ensure_ascii=False)[:250])
        print("disclaimer:", bool(final.get("disclaimer")))
    else:
        print("无 final")


asyncio.run(main())
