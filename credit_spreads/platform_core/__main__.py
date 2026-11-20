import sys

from .cli import main

if __name__ == "__main__":          # the quote recorder's spawned processes import this module too
    sys.exit(main())
