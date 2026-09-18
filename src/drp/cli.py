"""``drp`` command line: scan -> register -> baseline -> issue -> serve/worker -> validate."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import uvicorn
from sqlalchemy.orm import Session

from drp import metrics
from drp.baseline import establish_baseline
from drp.config import get_settings
from drp.db import init_db, session_scope
from drp.findings.scanner import hits_to_json, scan_paths
from drp.findings.spec import FindingSpec, register
from drp.github.client import GitHubClient
from drp.github.issues import open_issue
from drp.models import BaselineStatus, Finding
from drp.worker import Worker, validate_ref_offline


def _finding(session: Session, finding_id: str) -> Finding:
    f = session.get(Finding, finding_id)
    if f is None:
        sys.exit(f"unknown finding {finding_id!r}; run `drp register` first")
    return f


def cmd_scan(args: argparse.Namespace) -> None:
    root = Path(args.root)
    files = [root / p for p in args.paths] if args.paths else None
    hits = scan_paths(root, files)
    if args.kind:
        hits = [h for h in hits if h.kind in args.kind]
    print(hits_to_json(hits))


def cmd_register(args: argparse.Namespace) -> None:
    init_db()
    directory = Path(args.spec)
    if directory.is_file():
        directory = directory.parent
    spec = FindingSpec.load(directory)
    with session_scope() as s:
        f = register(s, directory, spec)
        print(
            json.dumps(
                {"id": f.id, "mutant_sha256": f.mutant_sha256, "baseline": f.baseline_status.value}
            )
        )


def cmd_baseline(args: argparse.Namespace) -> None:
    init_db()
    settings = get_settings()
    with session_scope() as s:
        f = _finding(s, args.finding)
        status, evidence = establish_baseline(f, settings)
        f.baseline_status = status
        f.baseline_evidence = evidence
        print(
            json.dumps(
                {"finding": f.id, "baseline": status.value, "evidence": evidence},
                indent=2,
                default=str,
            )
        )
    if status != BaselineStatus.CONFIRMED:
        sys.exit(2)


def cmd_issue(args: argparse.Namespace) -> None:
    init_db()
    settings = get_settings()
    with session_scope() as s:
        f = _finding(s, args.finding)
        if f.baseline_status != BaselineStatus.CONFIRMED:
            sys.exit(
                f"finding {f.id} baseline is {f.baseline_status.value}; refusing to open an issue"
            )
        if f.github_issue_url and not args.force:
            print(f.github_issue_url)
            return
        open_issue(f, settings, GitHubClient(settings))
        print(f.github_issue_url)


def cmd_serve(args: argparse.Namespace) -> None:
    init_db()
    uvicorn.run("drp.web.app:app", host=args.host, port=args.port, log_level="info")


def cmd_worker(args: argparse.Namespace) -> None:
    init_db()
    worker = Worker()
    if args.once:
        did = worker.run_once()
        print(json.dumps({"processed": did}))
    else:
        worker.run_forever()


def cmd_validate(args: argparse.Namespace) -> None:
    init_db()
    settings = get_settings()
    with session_scope() as s:
        f = _finding(s, args.finding)
        outcome = validate_ref_offline(f, args.ref, settings)
    print(json.dumps(outcome.to_dict(), indent=2, default=str))
    sys.exit(0 if outcome.verdict.value == "verified" else 3)


def cmd_show(args: argparse.Namespace) -> None:
    init_db()
    with session_scope() as s:
        print(json.dumps(metrics.compute(s).to_dict(), indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="drp", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="static weak-test scan of a pytest tree")
    s.add_argument("--root", default=str(get_settings().target_checkout))
    s.add_argument("--kind", action="append", help="filter by kind (repeatable)")
    s.add_argument("paths", nargs="*", help="test files relative to root (default: tests/)")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("register", help="register a finding + controlled regression")
    s.add_argument("spec", help="finding directory (containing finding.yaml + mutant.patch)")
    s.set_defaults(fn=cmd_register)

    s = sub.add_parser("baseline", help="establish the deterministic baseline (clean + mutant)")
    s.add_argument("finding")
    s.set_defaults(fn=cmd_baseline)

    s = sub.add_parser("issue", help="open the GitHub issue for a confirmed finding")
    s.add_argument("finding")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_issue)

    s = sub.add_parser("serve", help="run webhook receiver + dashboard")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("worker", help="run the durable remediation worker")
    s.add_argument("--once", action="store_true")
    s.set_defaults(fn=cmd_worker)

    s = sub.add_parser("validate", help="independently validate an arbitrary ref against a finding")
    s.add_argument("finding")
    s.add_argument("ref")
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("metrics", help="print metrics as JSON")
    s.set_defaults(fn=cmd_show)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args.fn(args)


if __name__ == "__main__":
    main()
