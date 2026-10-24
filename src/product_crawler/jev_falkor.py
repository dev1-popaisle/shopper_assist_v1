"""Price and review signals for Jev, read from the product_search graph."""

from __future__ import annotations

import re
from collections import defaultdict
from statistics import median
from typing import Any

from falkordb import FalkorDB

from product_crawler.falkor_sync import GRAPH_NAME, _falkor_settings


def pricing_review_analysis(product_ids: list[str]) -> dict[str, Any]:
    """Summarize selected products' graph prices, ratings, review sentiment, and brands."""
    url, password = _falkor_settings()
    client = FalkorDB.from_url(url, password=password, socket_connect_timeout=6, socket_timeout=15)
    try:
        graph = client.select_graph(GRAPH_NAME)
        selected_rows = graph.query("""
            UNWIND $ids AS id
            MATCH (p:Product {product_id: id})
            OPTIONAL MATCH (p)-[:MADE_BY]->(b:Brand)
            OPTIONAL MATCH (p)-[:HAS_PRICE {kind: 'current'}]->(current_price:Price)
            RETURN p.product_id, p.name, p.brand, b.name,
                   coalesce(current_price.amount, p.price), coalesce(current_price.currency, p.currency),
                   p.rating, p.review_count
        """, {"ids": list(dict.fromkeys(product_ids))}, timeout=15000).result_set
        review_rows = graph.query("""
            UNWIND $ids AS id
            MATCH (p:Product {product_id: id})
            OPTIONAL MATCH (p)-[:HAS_REVIEW]->(r:ProductReview)
            RETURN p.product_id, r.rating, r.text
        """, {"ids": list(dict.fromkeys(product_ids))}, timeout=15000).result_set
        brand_counts = graph.query("""
            MATCH (p:Product)-[:MADE_BY]->(b:Brand)
            RETURN b.name, count(p)
        """, timeout=15000).result_set
        pagerank_rows = graph.query("""
            CALL algo.pageRank('Brand', 'MADE_BY')
            YIELD node, score
            RETURN node.name, score
        """, timeout=15000).result_set
    finally:
        client.close()

    products: dict[str, dict[str, Any]] = {}
    for row in selected_rows:
        product_id, name, brand, brand_node, price, currency, rating, review_count = row
        if not product_id:
            continue
        products[str(product_id)] = {
            "id": str(product_id),
            "name": str(name or product_id),
            "brand": str(brand_node or brand or ""),
            "price": _number(price),
            "currency": str(currency or ""),
            "rating": _number(rating),
            "stored_review_count": _number(review_count),
        }
    if not products:
        raise RuntimeError("FalkorDB has no graph records for the selected products. Sync them before using this option.")

    reviews: dict[str, list[tuple[float | None, str]]] = defaultdict(list)
    for product_id, raw_rating, text in review_rows:
        if product_id and str(product_id) in products and (raw_rating is not None or text):
            reviews[str(product_id)].append((_number(raw_rating), str(text or "")))

    brand_product_counts = {str(row[0]): int(row[1]) for row in brand_counts if row[0]}
    brand_pagerank = {str(row[0]): float(row[1]) for row in pagerank_rows if row[0] and isinstance(row[1], (int, float))}
    comparable_prices: dict[str, list[float]] = defaultdict(list)
    for product in products.values():
        if product["price"] is not None and product["currency"]:
            comparable_prices[product["currency"].upper()].append(product["price"])

    analyzed: list[dict[str, Any]] = []
    for product in products.values():
        rated_reviews = [rating for rating, _text in reviews.get(product["id"], []) if rating is not None and 0 <= rating <= 5]
        sentiment_values = [max(-1.0, min(1.0, (rating - 3.0) / 2.0)) for rating in rated_reviews]
        if sentiment_values:
            sentiment_score = sum(sentiment_values) / len(sentiment_values)
            positive = sum(rating >= 4 for rating in rated_reviews)
            negative = sum(rating <= 2 for rating in rated_reviews)
            neutral = len(rated_reviews) - positive - negative
            sentiment_source = "individual review ratings"
        elif product["rating"] is not None:
            sentiment_score = max(-1.0, min(1.0, (product["rating"] - 3.0) / 2.0))
            positive = negative = neutral = 0
            sentiment_source = "aggregate product rating; individual review ratings unavailable"
        else:
            sentiment_score = None
            positive = negative = neutral = 0
            sentiment_source = "no rating data available"

        price_comments = [text for _rating, text in reviews.get(product["id"], []) if text]
        price_positive, price_negative = _price_comment_counts(price_comments)
        currency = product["currency"].upper()
        same_currency_prices = comparable_prices.get(currency, []) if currency else []
        price_median = median(same_currency_prices) if same_currency_prices else None
        price = product["price"]
        product["price_analysis"] = {
            "selected_set_median": price_median,
            "difference_from_selected_median_percent": round(((price - price_median) / price_median) * 100, 1) if price is not None and price_median else None,
            "rank_low_to_high": 1 + sum(other < price for other in same_currency_prices) if price is not None else None,
            "compared_products_same_currency": len(same_currency_prices),
        }
        product["review_sentiment"] = {
            "score_minus1_to_1": round(sentiment_score, 3) if sentiment_score is not None else None,
            "positive_reviews": positive,
            "neutral_reviews": neutral,
            "negative_reviews": negative,
            "rated_review_count": len(rated_reviews),
            "source": sentiment_source,
            "price_value_positive_mentions": price_positive,
            "price_value_negative_mentions": price_negative,
            "method_note": "Polarity is inferred from 1–5 star ratings; price/value comment mentions use a small transparent keyword list, not an NLP classifier.",
        }
        product["brand_graph_context"] = {
            "products_in_graph_for_brand": brand_product_counts.get(product["brand"], 0),
            "pagerank_centrality": round(brand_pagerank[product["brand"]], 8) if product["brand"] in brand_pagerank else None,
            "pagerank_note": "Structural graph centrality only; it is not a quality or sentiment score.",
        }
        product["stored_review_count"] = int(product["stored_review_count"] or 0)
        analyzed.append(product)

    analyzed.sort(key=lambda item: (item["price"] is None, item["price"] if item["price"] is not None else float("inf")))
    return {
        "source": "FalkorDB product_search graph",
        "scope": "Pricing, brand structure, and review signals only. Jev remains the decision-maker and must choose among the checked products.",
        "instructions_for_jev": "Use these signals only for price or value aspects of the user's question. Compare prices only within the same currency. Rating sentiment is evidence from customer reviews. Brand PageRank and product counts describe graph prominence, not product quality; do not use them as a quality score.",
        "brand_algorithm": "FalkorDB algo.pageRank over Brand nodes and MADE_BY relationships",
        "price_comparison_scope": "checked products with the same currency",
        "products": analyzed,
    }


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def _price_comment_counts(comments: list[str]) -> tuple[int, int]:
    positive_terms = {"affordable", "bargain", "inexpensive", "reasonable", "value", "worth", "worthwhile", "fairly priced"}
    negative_terms = {"expensive", "overpriced", "pricey", "costly", "not worth", "poor value"}
    positive = negative = 0
    for comment in comments:
        text = comment.casefold()
        if any(term in text for term in positive_terms):
            positive += 1
        if any(term in text for term in negative_terms):
            negative += 1
    return positive, negative
