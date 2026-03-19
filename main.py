import asyncio

from llama_index.core.agent import ReActAgent
from llama_index.core.tools import FunctionTool
from llama_index.llms.anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()


def multiply(a: float, b: float) -> float:
    """Multiply two numbers and return the result."""
    return a * b


def add(a: float, b: float) -> float:
    """Add two numbers and return the result."""
    return a + b


llm = Anthropic(model="claude-sonnet-4-6")
tools = [
    FunctionTool.from_defaults(fn=multiply),
    FunctionTool.from_defaults(fn=add),
]
agent = ReActAgent(tools=tools, llm=llm, verbose=True)


if __name__ == "__main__":
    async def main():
        response = await agent.run(user_msg="What is (3 + 5) * 12?")
        print("\nFinal answer:", response)

    asyncio.run(main())
