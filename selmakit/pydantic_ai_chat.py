import asyncio

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

MODEL = "llama3.2"
BASE_URL = "http://localhost:11434/v1"


async def main() -> None:
    model = OpenAIChatModel(MODEL, provider=OpenAIProvider(base_url=BASE_URL))
    agent = Agent(model=model)
    history = []

    print(f"Chat with {MODEL} (Ctrl+C or /quit to exit)\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in {"/quit", "/exit", "/q"}:
            break

        print("Assistant: ", end="", flush=True)
        async with agent.run_stream(user_input, message_history=history) as result:
            async for chunk in result.stream_text(delta=True):
                print(chunk, end="", flush=True)
            history = result.all_messages()
        print("\n")


if __name__ == "__main__":
    asyncio.run(main())
