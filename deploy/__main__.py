"""Explicit deployment operations. No commands execute during import."""

import argparse
import asyncio
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "models", "prepare", "polling", "polling-health", "core", "worker", "health"))
    parser.add_argument("role", nargs="?", choices=("core", "worker"))
    args = parser.parse_args()
    if args.command == "polling-health":
        from deploy.polling import healthy
        return 0 if healthy() else 1
    if args.command == "health":
        from deploy.server import healthy
        return 0 if args.role and healthy(args.role) else 1
    from deploy.config import validate
    validate()
    if args.command == "validate":
        print("Deployment configuration validated; secrets omitted", flush=True)
    elif args.command == "models":
        from deploy.models import prepare_models
        asyncio.run(prepare_models())
    elif args.command == "prepare":
        from deploy.bootstrap import prepare_storage
        asyncio.run(prepare_storage())
    elif args.command == "polling":
        from deploy.polling import run
        asyncio.run(run())
    else:
        from deploy.server import run
        return run(args.command)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Settings and HTTP exceptions can contain credentials; report safe diagnostics only.
        from rag.contracts import StepError
        detail = error.code if isinstance(error, StepError) else str(error) if type(error) in (RuntimeError, ValueError) else type(error).__name__
        print("Deployment failed: " + detail, file=sys.stderr, flush=True)
        sys.exit(1)
