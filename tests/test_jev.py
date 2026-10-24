import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from product_crawler.jev import decision_request, read_decision
from product_crawler.jev_insight import explain_decision
from product_crawler.server import create_app


LAMP = {
    "id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
    "name": "Prompt Lamp",
    "brand": "FENMZEE",
    "barcode": "012345678905",
    "barcode_type": "UPC-A",
    "source_url": "https://shop.example/lamp",
    "payload": {"price": 19.99, "currency": "USD", "description": {"text": "A small lamp."}, "features": ["Inline switch"]},
}
BUDS = {
    "id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
    "name": "JBL Vibe",
    "brand": "JBL",
    "barcode": None,
    "source_url": "https://example.com/jbl",
    "payload": {},
}


class JevDecisionTests(unittest.TestCase):
    def test_checked_products_are_the_choice_options(self) -> None:
        request = decision_request([LAMP, BUDS], "Which one is audio gear?")
        question = request["questions"]["product"]
        self.assertEqual(request["model"], "typesafe/jev-1.13")
        self.assertEqual(set(request["questions"]), {"product", "noul", "score"})
        self.assertEqual(question["type"], "choice")
        self.assertEqual(question["instructions"], "Which one is audio gear?")
        self.assertEqual(question["criteria"][BUDS["id"]], "JBL Vibe")
        self.assertEqual(question["criteria"][LAMP["id"]], "Prompt Lamp")
        self.assertEqual(request["questions"]["noul"]["type"], "noul")
        self.assertEqual(request["questions"]["noul"]["instructions"], "Which one is audio gear?")
        self.assertIn("true", request["questions"]["noul"]["criteria"])
        self.assertEqual(request["questions"]["score"]["type"], "score")
        self.assertEqual(request["questions"]["score"]["instructions"], "Which one is audio gear?")
        self.assertEqual(len(request["questions"]["score"]["criteria"]), 3)

    def test_read_decision_returns_the_pick_and_every_probability(self) -> None:
        decision = read_decision(
            {
                "model": "typesafe/jev-1.13-20260917",
                "answers": {
                    "product": {
                        "type": "choice",
                        "choice": BUDS["id"],
                        "confidence": 0.32,
                        "probabilities": {LAMP["id"]: 0.34, BUDS["id"]: 0.66},
                    },
                    "noul": {"type": "noul", "noul": 0.91},
                    "score": {
                        "type": "score",
                        "score": 1.43,
                        "confidence": 0.35,
                        "probabilities": {"0": 0.0, "1": 0.57, "2": 0.43},
                    },
                },
            },
            [LAMP, BUDS],
        )
        self.assertEqual(decision["product_name"], "JBL Vibe")
        self.assertAlmostEqual(decision["confidence"], 0.32)
        self.assertEqual(
            [(item["name"], item["probability"]) for item in decision["probabilities"]],
            [("JBL Vibe", 0.66), ("Prompt Lamp", 0.34)],
        )
        self.assertAlmostEqual(decision["noul"], 0.91)
        self.assertAlmostEqual(decision["score"]["score"], 1.43)
        self.assertAlmostEqual(decision["score"]["confidence"], 0.35)
        self.assertEqual(
            [(level["description"], level["probability"]) for level in decision["score"]["levels"]],
            [
                ("Does not match the question", 0.0),
                ("Partly matches the question", 0.57),
                ("Clearly matches the question", 0.43),
            ],
        )

    def test_decide_route_sends_the_checked_product(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            with patch("product_crawler.server.ask_jev", return_value={"choice": LAMP["id"], "product_name": "Prompt Lamp", "confidence": 1.0, "probabilities": [{"id": LAMP["id"], "name": "Prompt Lamp", "probability": 1.0}], "model": "typesafe/jev-1.13"}) as ask:
                with patch("product_crawler.server.explain_decision", return_value="Yes means the lamp fits the question.") as explain:
                    with patch("product_crawler.server.load_openrouter_key", return_value="test-key"):
                        with patch.object(app.state.store, "get", return_value=LAMP):
                            response = client.post(
                                "/jev/decide",
                                json={"ids": [LAMP["id"]], "question": "Is this a lamp?"},
                            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["product_name"], "Prompt Lamp")
        self.assertEqual(response.json()["insight"], "Yes means the lamp fits the question.")
        ask.assert_called_once()
        explain.assert_called_once()
        self.assertEqual(ask.call_args.args[1], "Is this a lamp?")

    def test_explain_decision_sends_the_question_and_jev_numbers(self) -> None:
        class Response:
            status_code = 200
            text = ""

            def json(self) -> dict:
                return {
                    "output": [
                        {"content": [{"text": "Yes means one of these shoes can work for the hike."}]}
                    ]
                }

        decision = {
            "product_name": "Moab 3 Mid Waterproof",
            "confidence": 1.0,
            "probabilities": [{"name": "Moab 3 Mid Waterproof", "probability": 1.0}],
            "noul": 0.76,
            "score": {
                "score": 1.79,
                "confidence": 0.69,
                "levels": [
                    {"description": "Does not match the question", "probability": 0.02},
                    {"description": "Partly matches the question", "probability": 0.18},
                    {"description": "Clearly matches the question", "probability": 0.80},
                ],
            },
        }
        question = "I need an outdoor shoe for hiking"
        with patch("product_crawler.jev_insight.load_api_key", return_value="sk-test"):
            with patch("product_crawler.jev_insight.httpx.post", return_value=Response()) as post:
                text = explain_decision(question, decision)
        self.assertEqual(text, "Yes means one of these shoes can work for the hike.")
        sent = post.call_args.kwargs["json"]["input"]
        self.assertIn(question, sent)
        self.assertIn("Moab 3 Mid Waterproof", sent)
        self.assertIn("Yes: 76%", sent)
        self.assertIn("No: 24%", sent)
        self.assertIn("1.79 of 2", sent)
        self.assertIn("69%", sent)

    def test_decide_route_requires_a_question_and_a_product(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            missing_question = client.post("/jev/decide", json={"ids": [LAMP["id"]], "question": "  "})
            missing_product = client.post("/jev/decide", json={"ids": [], "question": "Is this a lamp?"})
        self.assertEqual(missing_question.status_code, 400)
        self.assertEqual(missing_product.status_code, 400)


if __name__ == "__main__":
    unittest.main()
