"""Source-tree compatibility wrapper; installed users can use opencode-t3 resolve."""
import argparse
import json
from pathlib import Path

from opencode_t3.references import resolve


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference")
    parser.add_argument("--database", type=Path, default=Path.home() / ".t3/userdata/statev2.sqlite")
    parser.add_argument("--environment-id")
    args = parser.parse_args()
    envfile = args.database.parent / "environment-id"
    env = args.environment_id or (envfile.read_text().strip() if envfile.exists() else None)
    matches = resolve(args.database, args.reference, env)
    print(json.dumps(dict(reference=args.reference, matches=matches), indent=2))
    raise SystemExit(0 if matches else 1)
