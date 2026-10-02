"""Run the investigator.

  python run.py                                  # asks you before any change (interactive)
  python run.py --approval deny                  # read-only: proposes fixes but never applies them
  python run.py --approval auto-sandbox          # lab mode: auto-approves, sandbox namespace only
"""
import argparse
import sys

from anthropic import Anthropic

from investigator.agent import Investigator
from investigator.approvals import APPROVERS
from investigator.tools import KubeTools

DEFAULT_GOAL = "The report-api pods are slow. Find out why and fix it."


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("goal", nargs="?", default=DEFAULT_GOAL)
    ap.add_argument("--namespace", default="sandbox")
    ap.add_argument("--approval", default="ask", choices=sorted(APPROVERS))
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--max-steps", type=int, default=20)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    tools = KubeTools(args.namespace, APPROVERS[args.approval])
    agent = Investigator(Anthropic(max_retries=3), tools, model=args.model, max_steps=args.max_steps)
    print(f"Goal: {args.goal}\nNamespace: {args.namespace} | approval: {args.approval} | model: {args.model}\n")
    rep = agent.run(args.goal)
    print(f"\n{'=' * 60}\n{rep.text}\n{'=' * 60}")
    print(f"steps={rep.steps} tools={len(rep.tool_calls)} stopped={rep.stopped} "
          f"tokens={rep.input_tokens}/{rep.output_tokens}")
    return 0 if rep.stopped == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
