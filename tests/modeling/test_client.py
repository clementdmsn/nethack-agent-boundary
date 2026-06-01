from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import model.client as agent


class FakeCompletions:
    def __init__(self) -> None:
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="ok"),
                    )
                ]
            )
        ]


class FakeOpenAI:
    def __init__(self, **_kwargs) -> None:
        self.completions = FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


class ModelClientTests(unittest.TestCase):
    def test_ask_stream_does_not_keep_chat_history(self) -> None:
        with patch.object(agent, "OpenAI", FakeOpenAI):
            client = agent.ModelClient()
            client.ask_stream("first", on_delta=lambda _delta: None)
            client.ask_stream("second", on_delta=lambda _delta: None)

        requests = client.client.completions.requests

        self.assertEqual(len(requests), 2)
        self.assertEqual(
            requests[0]["messages"],
            [
                {"role": "system", "content": agent.SYSTEM_PROMPT},
                {"role": "user", "content": "first"},
            ],
        )
        self.assertEqual(
            requests[1]["messages"],
            [
                {"role": "system", "content": agent.SYSTEM_PROMPT},
                {"role": "user", "content": "second"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
