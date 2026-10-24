"""Ask OpenAI to explain a Jev Choice, Noul, and Score for the typed question."""

from __future__ import annotations

from typing import Any

import httpx

from product_crawler.openai_enrich import MODEL, load_api_key

OPENAI_URL = "https://api.openai.com/v1/responses"


def explain_decision(question: str, decision: dict[str, Any]) -> str:
    """Turn the typed question and Jev numbers into a short explanation."""
    key = load_api_key()
    if not key:
        raise RuntimeError("OPENAI_API_KEY_AVATAR is not in api_config.json")
    response = httpx.post(
        OPENAI_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "input": _prompt(question, decision),
            "max_output_tokens": 700,
        },
        timeout=90,
    )
    if response.status_code >= 400:
        detail = response.text.replace(key, "[redacted]")[:300]
        raise RuntimeError(f"OpenAI returned HTTP {response.status_code}: {detail}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("OpenAI did not return JSON") from exc
    text = _response_text(payload)
    if not text:
        raise RuntimeError("OpenAI returned no text")
    return text


def _prompt(question: str, decision: dict[str, Any]) -> str:
    score = decision.get("score") if isinstance(decision.get("score"), dict) else {}
    levels = score.get("levels") if isinstance(score.get("levels"), list) else []
    top = max(len(levels) - 1, 0)
    position = score.get("score")
    position_text = f"{float(position):.2f} of {top}" if isinstance(position, (int, float)) else ""
    noul = decision.get("noul")
    yes = _percent(noul) if isinstance(noul, (int, float)) else ""
    no = _percent(1 - float(noul)) if isinstance(noul, (int, float)) else ""
    options = "\n".join(
        f"- {item.get('name')}: {_percent(item.get('probability'))}"
        for item in decision.get("probabilities") or []
        if isinstance(item, dict)
    )
    level_lines = "\n".join(
        f"- {level.get('description')}: {_percent(level.get('probability'))}"
        for level in levels
        if isinstance(level, dict)
    )
    confidence = score.get("confidence")
    graph_analysis = decision.get("falkordb_analysis")
    if isinstance(graph_analysis, dict):
        pricing_rows = []
        for item in graph_analysis.get("products") or []:
            if not isinstance(item, dict):
                continue
            price = item.get("price")
            currency = item.get("currency") or "currency unavailable"
            price_analysis = item.get("price_analysis") if isinstance(item.get("price_analysis"), dict) else {}
            sentiment = item.get("review_sentiment") if isinstance(item.get("review_sentiment"), dict) else {}
            brand = item.get("brand_graph_context") if isinstance(item.get("brand_graph_context"), dict) else {}
            pricing_rows.append(
                f"- {item.get('name')}: current price={price if price is not None else 'unavailable'} {currency}; "
                f"same-currency selected-set median={price_analysis.get('selected_set_median')}; "
                f"difference from median={price_analysis.get('difference_from_selected_median_percent')}%; "
                f"price rank={price_analysis.get('rank_low_to_high')}; "
                f"rating-derived sentiment={sentiment.get('score_minus1_to_1')} from {sentiment.get('rated_review_count')} rated reviews "
                f"(positive={sentiment.get('positive_reviews')}, neutral={sentiment.get('neutral_reviews')}, negative={sentiment.get('negative_reviews')}); "
                f"price/value comment mentions: positive={sentiment.get('price_value_positive_mentions')}, negative={sentiment.get('price_value_negative_mentions')}; "
                f"brand graph product count={brand.get('products_in_graph_for_brand')}, PageRank={brand.get('pagerank_centrality')}"
            )
        graph_text = "\n".join(pricing_rows) or "No FalkorDB pricing rows were available."
        return f"""Explain this Jev pricing decision in plain language for the person who asked the question.

The question was:
{question.strip()}

Jev Choice, already shown on the page. Do not pick a different product.
Selected: {decision.get("product_name") or ""}
Choice confidence: {_percent(decision.get("confidence"))}
Probability for each option:
{options}

Jev Noul is the probability that the answer to the question is yes. Yes is that probability. No is the rest. There is no separate Noul confidence.
Yes: {yes}
No: {no}

Jev Score is a position on this ordered scale, from 0 to {top}:
0. Does not match the question
1. Partly matches the question
2. Clearly matches the question
Score: {position_text}
Score confidence: {_percent(confidence) if isinstance(confidence, (int, float)) else ""}
Probability for each level:
{level_lines}

FalkorDB pricing and review evidence supplied to Jev:
{graph_text}

Write four short paragraphs and nothing else. First explain Jev's selected product, choice confidence, and the probability spread. Second explain Jev's Yes/No and Score outputs for this question. Third compare the checked products using only same-currency prices and the rating-derived review sentiment and price/value comment counts above; say where those signals agree or disagree with Jev's selection. Fourth give a practical pricing-focused reading. Do not claim that any single graph signal caused Jev's choice, do not treat brand PageRank as product quality, and do not invent data. Sentiment is rating-derived; price/value comment counts are simple keyword matches, not an NLP sentiment classifier.
"""

    return f"""Explain this Jev decision in plain sentences for the person who asked the question.

The question was:
{question.strip()}

Jev Choice, already shown on the page. Do not pick a different product.
Selected: {decision.get("product_name") or ""}
Choice confidence: {_percent(decision.get("confidence"))}
Probability for each option:
{options}

Jev Noul is the probability that the answer to the question is yes. Yes is that probability. No is the rest. There is no separate Noul confidence.
Yes: {yes}
No: {no}

Jev Score is a position on this ordered scale, from 0 to {top}:
0. Does not match the question
1. Partly matches the question
2. Clearly matches the question
Score: {position_text}
Score confidence: {_percent(confidence) if isinstance(confidence, (int, float)) else ""}
Probability for each level:
{level_lines}

Write three short paragraphs and nothing else.
First paragraph: say what Yes {yes} and No {no} mean for this exact question and these options.
Second paragraph: say what the score {position_text}, the score confidence, and each level probability mean for this exact question.
Third paragraph: give a practical reading of the selected product in light of those numbers.
Use the question and the numbers above. Do not invent specifications, prices, or reviews.
"""


def _percent(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    return f"{round(float(value) * 100)}%"


def _response_text(payload: dict[str, Any]) -> str:
    texts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"].strip())
    return "\n\n".join(part for part in texts if part)
