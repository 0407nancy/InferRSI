import argparse
import os
from pathlib import Path

from inferrsi_loop.protocol import main
from support import environment, load

if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project", required=True)
    args, _ = parser.parse_known_args()
    env = environment(args.project, load(Path(__file__).with_name("h3.json"))["environment"])
    os.environ.clear()
    os.environ.update(env)
    raise SystemExit(main())
