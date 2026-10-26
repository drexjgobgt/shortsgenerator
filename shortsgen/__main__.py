"""Entry point: `python -m shortsgen ...`"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
