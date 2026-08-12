#!/usr/bin/env python3

from __future__ import annotations

import json
import pathlib
import shutil
import tempfile

import ansible_runner


def main() -> None:
    private_data_dir = pathlib.Path(tempfile.mkdtemp(prefix="runner-"))
    try:
        run(private_data_dir)
    finally:
        shutil.rmtree(private_data_dir, ignore_errors=True)


def run(private_data_dir: pathlib.Path) -> None:
    r = ansible_runner.run(
        private_data_dir=str(private_data_dir),
        playbook=str(pathlib.Path(__file__).parent.parent / "playbooks" / "main.yml"),
        inventory="localhost ansible_connection=local",
        extravars={
        },
    )

    print("\n=== Events ===\n")
    for event in r.events:
        event_json = json.dumps(event, indent=2)
        print(f"Event [{event.get('event', 'unknown')}]")
        print(event_json)
        print()

    print("\n=== stdout ===\n")
    stdout = r.stdout.read()
    print(stdout)


if __name__ == "__main__":
    main()
