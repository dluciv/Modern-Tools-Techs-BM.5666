#!/usr/bin/env -S uv run --quiet --script --
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "requests>=2.34.2",
# ]
# ///
"""
Simple AI agent
"""
import requests
import json

from safe_agent_tools import VirtualFileSystem, DocumentationSearch, SafeCalculator

class SafeAgent:
    def __init__(self, api_url="http://localhost:8000/v1", model_name="markov-chain-v1", api_key=None):
        self.api_url = api_url
        self.model_name = model_name
        self.api_key = api_key
        self.conversation = []

        # Инициализируем безопасные инструменты
        self.fs = VirtualFileSystem()
        self.calc = SafeCalculator()
        self.docs = DocumentationSearch()

        self.tools_description = """You have access to the following tools:

1. list_files() - List all available files in the virtual filesystem
2. read_file(filename) - Read the contents of a specific file
3. calculate(expression) - Evaluate a mathematical expression (e.g., "2 + 2", "10 * 5")
4. factorial(n) - Calculate factorial of a number
5. search_docs(query) - Search Python documentation

To use a tool, respond with JSON in this exact format:
{"tool": "tool_name", "args": {"arg_name": "value"}}

Example:
{"tool": "calculate", "args": {"expression": "5 * 3 + 10"}}
{"tool": "read_file", "args": {"filename": "README.md"}}
{"tool": "search_docs", "args": {"query": "sorted"}}

If you don't need a tool, respond normally."""

    def add_system_message(self, content):
        self.conversation.append({
            "role": "system",
            "content": self.tools_description + "\n\n" + content
        })

    def chat(self, user_message):
        self.conversation.append({"role": "user", "content": user_message})

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        response = requests.post(
            f"{self.api_url}/chat/completions",
            headers=headers,
            json={
                "model": self.model_name,
                "messages": self.conversation,
                "max_tokens": 200
            }
        )

        assistant_message = response.json()["choices"][0]["message"]["content"]
        self.conversation.append({"role": "assistant", "content": assistant_message})

        return assistant_message

    def execute_tool(self, tool_call):
        tool_name = tool_call.get("tool")
        args = tool_call.get("args", {})

        try:
            if tool_name == "list_files":
                return self.fs.list_files()
            elif tool_name == "read_file":
                filename = args.get("filename")
                return self.fs.read_file(filename)
            elif tool_name == "calculate":
                expression = args.get("expression")
                return self.calc.calculate(expression)
            elif tool_name == "factorial":
                n = args.get("n")
                return self.calc.factorial(n)
            elif tool_name == "search_docs":
                query = args.get("query")
                return self.docs.search(query)
            else:
                return f"Error: Unknown tool '{tool_name}'"
        except Exception as e:
            return f"Error executing tool: {str(e)}"

    def run(self):
        print(f"Safe Agent started with model: {self.model_name}")
        print("Available tools: list_files, read_file, calculate, factorial, search_docs")
        print("Type 'quit' to exit.\n")

        self.add_system_message("You are a helpful assistant that can use tools to answer questions.")

        while True:
            user_input = input("\nYou: ")
            if user_input.lower() == 'quit':
                break

            response = self.chat(user_input)
            print(f"Agent: {response}")

            # Пытаемся извлечь и выполнить tool call
            try:
                if '{' in response and '}' in response:
                    start = response.find('{')
                    end = response.rfind('}') + 1
                    json_str = response[start:end]
                    tool_call = json.loads(json_str)

                    if "tool" in tool_call:
                        print(f"\n[Executing tool: {tool_call['tool']}]")
                        result = self.execute_tool(tool_call)
                        print(f"[Result: {result}]\n")

                        # Возвращаем результат модели
                        self.chat(f"Tool execution result: {result}")
            except json.JSONDecodeError:
                pass
            except Exception as e:
                print(f"[Tool execution error: {e}]")

if __name__ == "__main__":
    # Примеры использования с разными моделями:

    # 1. Марковская цепь (глупая модель)
    # agent = SafeAgent(api_url="http://localhost:8000/v1", model_name="markov-chain-v1")

    # 2. Локальная Ollama (нормальная модель)
    # agent = SafeAgent(api_url="http://localhost:11434/v1", model_name="llama3.2")

    # 3. OpenAI
    # agent = SafeAgent(api_url="https://api.openai.com/v1", model_name="gpt-4", api_key="sk-...")

    agent = SafeAgent()
    agent.run()
