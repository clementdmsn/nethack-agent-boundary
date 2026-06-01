from openai import OpenAI
from collections.abc import Callable

from config import API_KEY, BASE_URL, MODEL
from prompts import SYSTEM_PROMPT


class ModelClient:
    def __init__(self) -> None:
        # Creates the OpenAI-compatible client used for model calls.
        self.client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

    def ask_stream(
        self,
        prompt: str,
        on_delta: Callable[[str], None] | None = None,
    ) -> str:
        # Sends one prompt to the model and streams the text response back.
        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": prompt,
            },
        ]

        stream = self.client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.2,
            stream=True,
        )

        full_response = ""

        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                if on_delta is None:
                    print(delta, end="", flush=True)
                else:
                    on_delta(delta)
                full_response += delta

        if on_delta is None:
            print()

        return full_response.strip()
