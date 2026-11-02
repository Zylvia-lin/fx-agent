"""Keep CLI output separate from the stdio MCP protocol."""

import argparse
import logging

from fx_agent import __version__


def main() -> None:
    """Start the MCP service, or handle explicit CLI options without connecting."""
    parser = argparse.ArgumentParser(description="FX Agent local MCP workstation")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    from fx_agent.server import get_server

    get_server().run(transport="stdio")


if __name__ == "__main__":
    main()
