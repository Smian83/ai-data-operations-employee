"""Entrypoint for `python -m app.worker`."""
from app.worker.runner import run_forever

if __name__ == "__main__":
    run_forever()
