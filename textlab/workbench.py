"""Local operator CLI; training is never invoked from the public web service."""

import argparse
import json
import os
from pathlib import Path
import secrets
from textlab.dataflow import DataStore
from textlab import experiments

DEFAULT_EPOCHS = 20
DEFAULT_THRESHOLD = 0.5
DEFAULT_PORT = 8765
OPERATOR_TOKEN_BYTES = 32
PRIVATE_FILE_MODE = 0o600


def main() -> None:
    """Run one explicit operator action so training stays outside public HTTP."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("SMS_LAB_ROOT", "var")))
    cmd = parser.add_subparsers(dest="command", required=True)
    cmd.add_parser("prepare")
    cmd.add_parser("snapshot")
    cmd.add_parser("runs")
    cmd.add_parser("status")
    cmd.add_parser("create-token")
    p = cmd.add_parser("preview")
    p.add_argument("csv", type=Path)
    p.add_argument("--source", required=True)
    p = cmd.add_parser("confirm")
    p.add_argument("preview_id")
    p = cmd.add_parser("review")
    p.add_argument("sample_id")
    p.add_argument("--label", choices=["ham", "spam"], required=True)
    p.add_argument("--note", required=True)
    p.add_argument("--revision", type=int, required=True)
    p = cmd.add_parser("train")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    p = cmd.add_parser("select")
    p.add_argument("--run", required=True)
    p.add_argument("--kind", choices=["lr", "mlp16"], default="lr")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--revision", type=int)
    p = cmd.add_parser("rollback")
    p.add_argument("--revision", type=int)
    p = cmd.add_parser("export-public")
    p.add_argument("destination", type=Path)
    p = cmd.add_parser("freeze-final")
    p.add_argument("--run", required=True)
    p.add_argument("--kind", choices=["lr", "mlp16"], default="lr")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--reason", required=True)
    cmd.add_parser("evaluate-final")
    p = cmd.add_parser("serve")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--public", action="store_true")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        os.environ["SMS_LAB_ROOT"] = str(args.root.resolve())
        os.environ["SMS_LAB_PUBLIC"] = "true" if args.public else "false"
        if not args.public:
            token_path = args.root / "operator-token.txt"
            if not os.environ.get("SMS_LAB_ADMIN_TOKEN"):
                if not token_path.exists():
                    raise ValueError(
                        "Run create-token first; read it privately into the browser memory field"
                    )
                os.environ["SMS_LAB_ADMIN_TOKEN"] = token_path.read_text().strip()
        uvicorn.run(
            "textlab.app_v2:create_app",
            factory=True,
            host="127.0.0.1",
            port=args.port,
            workers=1,
            access_log=False,
        )
        return
    store = DataStore(args.root)
    if args.command == "prepare":
        result = store.prepare()
    elif args.command == "snapshot":
        result = store.snapshot()
    elif args.command == "preview":
        result = store.preview_csv(args.csv.read_text(encoding="utf-8-sig"), args.source)
    elif args.command == "confirm":
        result = store.confirm_import(args.preview_id)
    elif args.command == "review":
        result = store.review(args.sample_id, args.label, args.note, args.revision)
    elif args.command == "train":
        result = experiments.train(args.root, args.snapshot, args.run, args.epochs)
    elif args.command == "select":
        result = experiments.select(args.root, args.run, args.kind, args.threshold, args.revision)
    elif args.command == "rollback":
        result = experiments.rollback(args.root, args.revision)
    elif args.command == "export-public":
        result = experiments.export_public(args.root, args.destination)
    elif args.command == "freeze-final":
        from textlab.evaluation import freeze_final

        result = freeze_final(args.root, args.run, args.kind, args.threshold, args.reason)
    elif args.command == "evaluate-final":
        from textlab.evaluation import evaluate_final

        result = evaluate_final(args.root)
    elif args.command == "runs":
        result = {"runs": experiments.list_runs(args.root)}
    elif args.command == "status":
        result = experiments.registry(args.root)
    elif args.command == "create-token":
        path = args.root / "operator-token.txt"
        with path.open("x") as stream:
            stream.write(secrets.token_urlsafe(OPERATOR_TOKEN_BYTES) + "\n")
        path.chmod(PRIVATE_FILE_MODE)
        result = {
            "token_file": str(path.resolve()),
            "scope": "read privately into SMS_LAB_ADMIN_TOKEN; never commit or paste it",
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
