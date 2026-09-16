"""Create a clean portable source snapshot, without running research jobs."""
import argparse,json
from lab.release import build_snapshot
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True)
    args=parser.parse_args();print(json.dumps(build_snapshot(args.output),ensure_ascii=False,indent=2))
