"""Ask OpenRouter Jev a decision about the products checked on the page."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.13"
SCORE_LEVELS = (
    "Does not match the question",
    "Partly matches the question",
    "Clearly matches the question",
)


def load_openrouter_key() -> str | None:
    for path in (Path("api_config.json"), Path(__file__).resolve().parents[2] / "api_config.json"):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        key = payload.get("OPENROUTER_API_KEY") if isinstance(payload, dict) else None
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def ask_jev(
    products: list[dict[str, Any]],
    question: str,
    api_key: str,
    falkordb_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask Jev for a Choice, a Noul, and a Score on the typed question."""
    response = httpx.post(
        JEV_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=decision_request(products, question, falkordb_analysis=falkordb_analysis),
        timeout=60,
    )
    if response.status_code >= 400:
        detail = response.text.replace(api_key, "[redacted]")[:300]
        raise RuntimeError(f"Jev returned HTTP {response.status_code}: {detail}")
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError("Jev did not return JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Jev did not return a decision")
    return read_decision(payload, products)


def decision_request(
    products: list[dict[str, Any]],
    question: str,
    *,
    falkordb_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = question.strip()
    instructions = text
    state: dict[str, Any] = {"products": [_product_state(product) for product in products]}
    if falkordb_analysis is not None:
        state["falkordb_pricing_review_analysis"] = falkordb_analysis
        instructions += (
            "\n\nPricing-only graph guidance: if the question asks about price, cost, affordability, or value, "
            "use state.falkordb_pricing_review_analysis to compare the checked products. Compare prices only "
            "within the same currency and treat review-rating sentiment as review evidence. Brand PageRank is "
            "structural prominence, not quality. Ignore this graph context for non-pricing criteria and choose "
            "only among the checked products."
        )
    return {
        "model": JEV_MODEL,
        "state": state,
        "questions": {
            "product": {
                "type": "choice",
                "instructions": instructions,
                "criteria": {
                    str(product["id"]): _product_label(product)
                    for product in products
                },
            },
            "noul": {
                "type": "noul",
                "instructions": text,
                "criteria": {
                    "true": "Yes. The checked products satisfy this question.",
                    "false": "No. The checked products do not satisfy this question.",
                },
            },
            "score": {
                "type": "score",
                "instructions": text,
                "criteria": list(SCORE_LEVELS),
            },
        },
    }


def read_decision(payload: dict[str, Any], products: list[dict[str, Any]]) -> dict[str, Any]:
    answers = payload.get("answers")
    picked = answers.get("product") if isinstance(answers, dict) else None
    if not isinstance(picked, dict) or picked.get("choice") is None:
        raise RuntimeError("Jev did not return a choice")
    names = {str(product["id"]): _product_label(product) for product in products}
    choice_id = str(picked["choice"])
    raw_probabilities = picked.get("probabilities") if isinstance(picked.get("probabilities"), dict) else {}
    probabilities = []
    for product_id, name in names.items():
        raw = raw_probabilities.get(product_id)
        probabilities.append(
            {
                "id": product_id,
                "name": name,
                "probability": float(raw) if isinstance(raw, (int, float)) else 0.0,
            }
        )
    probabilities.sort(key=lambda item: item["probability"], reverse=True)
    confidence = picked.get("confidence")
    return {
        "choice": choice_id if choice_id in names else None,
        "product_name": names.get(choice_id),
        "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
        "probabilities": probabilities,
        "noul": _read_noul(answers),
        "score": _read_score(answers),
        "model": payload.get("model"),
    }


def _read_noul(answers: dict[str, Any]) -> float:
    answer = answers.get("noul") if isinstance(answers, dict) else None
    value = answer.get("noul") if isinstance(answer, dict) else None
    if not isinstance(value, (int, float)):
        raise RuntimeError("Jev did not return a noul")
    return float(value)


def _read_score(answers: dict[str, Any]) -> dict[str, Any]:
    answer = answers.get("score") if isinstance(answers, dict) else None
    position = answer.get("score") if isinstance(answer, dict) else None
    if not isinstance(position, (int, float)):
        raise RuntimeError("Jev did not return a score")
    raw = answer.get("probabilities") if isinstance(answer.get("probabilities"), dict) else {}
    legend = answer.get("legend") if isinstance(answer.get("legend"), dict) else {}
    levels = []
    for index, description in enumerate(SCORE_LEVELS):
        key = str(index)
        raw_probability = raw.get(key)
        if raw_probability is None:
            raw_probability = raw.get(index)
        label = legend.get(key, legend.get(index, description))
        levels.append(
            {
                "level": index,
                "description": str(label),
                "probability": float(raw_probability) if isinstance(raw_probability, (int, float)) else 0.0,
            }
        )
    confidence = answer.get("confidence")
    return {
        "score": float(position),
        "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
        "levels": levels,
    }


def _product_label(product: dict[str, Any]) -> str:
    name = product.get("name") or product.get("id")
    return str(name)


def _product_state(product: dict[str, Any]) -> dict[str, Any]:
    payload = product.get("payload") if isinstance(product.get("payload"), dict) else {}
    description = payload.get("description")
    text = description.get("text") if isinstance(description, dict) else description
    return {
        "id": product.get("id"),
        "name": product.get("name"),
        "brand": product.get("brand"),
        "barcode": product.get("barcode"),
        "barcode_type": product.get("barcode_type"),
        "source_url": product.get("source_url"),
        "price": payload.get("price"),
        "currency": payload.get("currency"),
        "description": text if isinstance(text, str) else None,
        "features": payload.get("features") if isinstance(payload.get("features"), list) else [],
        "specs": payload.get("specs") if isinstance(payload.get("specs"), dict) else {},
        "sku": payload.get("sku"),
    }
