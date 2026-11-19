
import json, sys
from . import pipeline

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--report" in argv:
        from .identity import review_report
        print(json.dumps(review_report(), ensure_ascii=False, indent=2))
        return
    mode = "full"
    if "--incremental" in argv:
        mode = "incremental"
    if "--full" in argv:
        mode = "full"
    print(json.dumps(pipeline.run(mode=mode), ensure_ascii=False))

if __name__ == "__main__":
    main()
