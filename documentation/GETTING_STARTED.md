# Getting started

This guide runs the Product Crawler and its local web UI from the repository root.

## Requirements

- Python 3.11 or newer
- A terminal opened in the project directory
- Account details configured for the local UI in `configs/user_accounts.rtf`

Some optional actions also need API credentials in `api_config.json`. Prompt-based
product search needs the configured OpenAI API key. Jev decisions need the
configured OpenRouter API key. BrightData enrichment and FalkorDB analysis are
optional integrations and require their own configuration.

## Install

Create and activate a virtual environment, then install the project in editable
mode:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

## Start the web UI

From the repository root, run:

```bash
python3 -m product_crawler.server
```

Alternatively, after installation:

```bash
product-server
```

The server listens on `127.0.0.1:8888`. Open
[http://127.0.0.1:8888](http://127.0.0.1:8888) in a browser and log in with a
configured account. The server is bound to the local machine, not all network
interfaces.

## Use the dashboard

1. **Crawl product URLs:** Enter a product page URL in **New URL**, then select
   **Add URL**. Use **BrightData enrichment** if configured and desired, then
   select **Run** to process pending entries from `urls.txt`.
2. **Search from a description:** In **Product Search by OpenAI API (Only)**,
   describe the product and select **Search**. This uses the configured OpenAI
   API; it does not run the built-in URL crawler. BrightData enrichment can be
   selected separately.
3. **Review results:** Select a product name to open its detail page. On the
   dashboard, check products and choose **Delete selected** to remove selected
   records and their local output files.
4. **Compare products:** Select one or more products, enter a question in
   **Decision Engine by Jev API (Only)**, and choose **Decide**. For pricing
   questions, optionally enable FalkorDB price and review analysis.

The **Reset** button clears the dashboard's current form inputs, selections,
decision, and status message. It does not delete stored products.

## Where data is stored

- `urls.txt` holds URL-crawl inputs and records UUIDs for successfully processed
  entries.
- `output/<uuid>/` contains each product's `product.json`, image manifest, and
  downloaded images.
- `data/products.duckdb` stores the product rows read by the UI.

The server imports product folders that are not yet present in DuckDB when it
starts. See the [UI overview](UI_OVERVIEW.md) for the pages, feature areas, and
their data flow.
