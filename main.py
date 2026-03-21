import asyncio

from llama_index.core import SimpleDirectoryReader, SummaryIndex
from llama_index.core.agent.workflow import FunctionAgent
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


async def answer_file_question(file_path: str, question: str) -> str:
    """Load a file from the given path and answer a question about its contents.
    Use this tool whenever the user asks about an uploaded file.
    file_path: absolute path to the file on disk
    question: the user's question about the file
    """
    docs = SimpleDirectoryReader(input_files=[file_path]).load_data()
    index = SummaryIndex.from_documents(docs)
    query_engine = index.as_query_engine(llm=llm, response_mode="tree_summarize")
    response = await query_engine.aquery(question)
    return str(response)


tools = [
    FunctionTool.from_defaults(fn=multiply),
    FunctionTool.from_defaults(fn=add),
    FunctionTool.from_defaults(async_fn=answer_file_question),
]
agent = FunctionAgent(tools=tools, llm=llm, verbose=True)


if __name__ == "__main__":
    async def main():
        response = await agent.run(user_msg="What is (3 + 5) * 12?")
        print("\nFinal answer:", response)

    asyncio.run(main())
