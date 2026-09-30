"""VlmGate: chat ahead of background, re-entrant within one task (an Ask must never wait on itself)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nvr.synopsis import VlmGate  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def test_nested_chat_turn_does_not_deadlock_and_releases():
    """The Ask handler and the router both take a chat turn on the same task: the inner one passes through, and
    a synopsis queued behind them runs once the Ask finishes (this froze every synopsis for hours once)."""
    async def main():
        g = VlmGate()
        order = []

        async def ask():
            async with g.chat():
                order.append("ask-outer")
                async with g.chat():          # what vlmroute does inside chat_stream
                    order.append("ask-inner")
                    await asyncio.sleep(0.05)

        async def synopsis():
            await asyncio.sleep(0.01)         # arrives while the Ask holds the gate
            async with g.background():
                order.append("synopsis")

        await asyncio.wait_for(asyncio.gather(ask(), synopsis()), 2)
        assert order == ["ask-outer", "ask-inner", "synopsis"]
        assert not g.lock.locked() and g.chat_waiting == 0 and g.no_chat.is_set()
    run(main())


def test_chat_goes_ahead_of_waiting_background():
    async def main():
        g = VlmGate()
        order = []

        async def bg(name, hold):
            async with g.background():
                order.append(name)
                await asyncio.sleep(hold)

        async def chat():
            await asyncio.sleep(0.01)
            async with g.chat():
                order.append("chat")

        first = asyncio.create_task(bg("bg1", 0.05))
        await asyncio.sleep(0)
        await asyncio.wait_for(asyncio.gather(first, bg("bg2", 0), chat()), 2)
        assert order == ["bg1", "chat", "bg2"]
    run(main())


def test_background_nested_in_background_passes_through():
    async def main():
        g = VlmGate()
        async with g.background():
            async with g.background():
                assert g.lock.locked()
        assert not g.lock.locked()
    run(main())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
