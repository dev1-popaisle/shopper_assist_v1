"""Command-line entry point.

The crawler reads a URL file. Text files contain one product URL per line.
JSON files contain a list of URL objects. Each successful crawl writes the
UUID output folder name back next to that URL.
"""

from __future__ import annotations

import sys
from pathlib import Path

from product_crawler.crawl import crawl
from product_crawler.url_file import UrlFileError, run_file

USAGE = "usage: product-crawler <urls-file>"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    if len(args) != 2:
        print(
            "error: a URL file is required. "
            "Nothing was fetched and no output folder was created.\n"
            f"{USAGE}",
            file=sys.stderr,
        )
        return 2

    path = Path(args[1])
    if not path.is_file():
        print(
            f"error: URL file not found: {path}\n{USAGE}",
            file=sys.stderr,
        )
        return 2

    try:
        return run_file(path, crawl)
    except UrlFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
