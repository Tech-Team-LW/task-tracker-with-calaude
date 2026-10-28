"""Root entrypoint: `python main.py <command> [options]`."""

from task_tracker.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
