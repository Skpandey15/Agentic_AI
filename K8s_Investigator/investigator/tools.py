"""Kubernetes tools for the investigator agent.

Safety model (enforced in code, not just in the prompt):
  * Only 'sandbox' / 'sandbox-<suffix>' namespaces can be touched at all.
  * Every kubectl call is a list of arguments (no shell), with names validated by regex.
  * Read tools are read-only. The ONLY write is propose_fix, which (a) validates the patch
    against a small allowlist and (b) requires the injected approver to say yes.
"""
import base64
import json
import re
import subprocess
from typing import Callable

SAFE_NS_RE = re.compile(r"^sandbox(-[a-z0-9][a-z0-9-]{0,40})?$")  # "sandbox" or "sandbox-<suffix>" only


def is_safe_namespace(ns: object) -> bool:
    return isinstance(ns, str) and bool(SAFE_NS_RE.match(ns))

NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,61}[a-z0-9])?$")
PATH_RE = re.compile(r"^/[A-Za-z0-9_\-/]{0,60}$")
DESCRIBE_KINDS = frozenset({"pod", "deployment", "replicaset", "service", "hpa"})
RESOURCE_KEYS = frozenset({"cpu", "memory"})
MAX_CHARS = 6000


class ToolError(Exception):
    """Returned to the model as an error result so it can adapt."""


def _subprocess_runner(cmd: list[str], stdin: str | None, timeout: float):
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def clip(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.4), int(limit * 0.5)
    return f"{text[:head]}\n...[truncated {len(text) - head - tail} chars]...\n{text[-tail:]}"


def validate_patch(patch: object) -> dict:
    """Allow only: spec.replicas (1-5) and, per container, name + resources + env."""
    def bad(msg):
        raise ToolError(f"Patch rejected: {msg}. Allowed: spec.replicas (1-5) and "
                        "spec.template.spec.containers[{name, resources{limits,requests}{cpu,memory}, env}]")

    if not isinstance(patch, dict) or set(patch) != {"spec"} or not isinstance(patch["spec"], dict):
        bad("top level must be exactly {'spec': {...}}")
    spec = patch["spec"]
    if set(spec) - {"replicas", "template"}:
        bad(f"unsupported spec keys {sorted(set(spec) - {'replicas', 'template'})}")
    if "replicas" in spec and not (isinstance(spec["replicas"], int) and 1 <= spec["replicas"] <= 5):
        bad("replicas must be an integer from 1 to 5")
    if "template" in spec:
        tmpl = spec["template"]
        if not isinstance(tmpl, dict) or set(tmpl) != {"spec"} or not isinstance(tmpl["spec"], dict) \
                or set(tmpl["spec"]) != {"containers"} or not isinstance(tmpl["spec"]["containers"], list):
            bad("template must be {'spec': {'containers': [...]}}")
        for c in tmpl["spec"]["containers"]:
            if not isinstance(c, dict) or not isinstance(c.get("name"), str) or set(c) - {"name", "resources", "env"}:
                bad("each container needs a 'name' and may only set 'resources' and 'env'")
            res = c.get("resources", {})
            if not isinstance(res, dict) or set(res) - {"limits", "requests"}:
                bad("resources may only contain 'limits' and 'requests'")
            for group in res.values():
                if not isinstance(group, dict) or set(group) - RESOURCE_KEYS:
                    bad("limits/requests may only set cpu and memory")
            for e in c.get("env", []):
                if not isinstance(e, dict) or set(e) - {"name", "value"} or "name" not in e:
                    bad("env entries may only be {name, value}")
    return patch


class KubeTools:
    def __init__(self, namespace: str, approver: Callable[[dict], bool],
                 runner=_subprocess_runner, base: tuple[str, ...] = ("wsl", "-e", "kubectl")):
        if not is_safe_namespace(namespace):
            raise ValueError(f"namespace '{namespace}' is not allowed; only 'sandbox' or 'sandbox-<suffix>'")
        self.ns, self._approve, self._run, self._base = namespace, approver, runner, tuple(base)

    # ---- plumbing ----
    def _kubectl(self, args: list[str], stdin: str | None = None, timeout: float = 45) -> str:
        try:
            code, out, err = self._run([*self._base, *args], stdin, timeout)
        except subprocess.TimeoutExpired:
            raise ToolError(f"kubectl timed out after {timeout}s")
        if code != 0:
            raise ToolError(clip((err or out).strip() or f"kubectl exited with {code}"))
        return clip(out)

    @staticmethod
    def _name(value: object, what: str) -> str:
        if not isinstance(value, str) or not NAME_RE.match(value):
            raise ToolError(f"invalid {what}: must be a lowercase Kubernetes name")
        return value

    # ---- read-only tools ----
    def list_pods(self) -> str:
        return self._kubectl(["get", "pods", "-o", "wide", "-n", self.ns])

    def describe(self, kind: str, name: str) -> str:
        if kind not in DESCRIBE_KINDS:
            raise ToolError(f"kind must be one of {sorted(DESCRIBE_KINDS)}")
        return self._kubectl(["describe", kind, self._name(name, "name"), "-n", self.ns])

    def get_logs(self, pod: str, tail: int = 100, previous: bool = False) -> str:
        tail = max(1, min(int(tail), 200))
        args = ["logs", self._name(pod, "pod"), f"--tail={tail}", "-n", self.ns]
        if previous:
            args.append("--previous")
        return self._kubectl(args)

    def top_pods(self) -> str:
        return self._kubectl(["top", "pod", "-n", self.ns])

    def get_events(self) -> str:
        return self._kubectl(["get", "events", "--sort-by=.lastTimestamp", "-n", self.ns])

    def probe_latency(self, pod: str, path: str = "/health", count: int = 5) -> str:
        """Time `count` GETs to localhost:8000<path> from inside the pod (fixed, harmless snippet)."""
        if not isinstance(path, str) or not PATH_RE.match(path):
            raise ToolError("path must look like /health or /api/items (no query string)")
        count = max(1, min(int(count), 8))
        snippet = (
            "import time,urllib.request,json\n"
            "r=[]\n"
            f"for _ in range({count}):\n"
            " s=time.perf_counter()\n"
            " try:\n"
            f"  urllib.request.urlopen('http://127.0.0.1:8000{path}',timeout=30).read();ok='ok'\n"
            " except Exception as e: ok=type(e).__name__\n"
            " r.append([round((time.perf_counter()-s)*1000),ok])\n"
            "print(json.dumps({'path':" + repr(path) + ",'results_ms_status':r}))\n"
        )
        b64 = base64.b64encode(snippet.encode()).decode()
        return self._kubectl(["exec", self._name(pod, "pod"), "-n", self.ns, "--",
                              "python", "-c", f"import base64;exec(base64.b64decode('{b64}'))"], timeout=90)

    def cpu_throttling(self, pod: str) -> str:
        """Kernel CPU throttle counters for the pod's container (cgroup cpu.stat). Read-only, fixed command."""
        snippet = (
            "import os\n"
            "for p in ('/sys/fs/cgroup/cpu.stat','/sys/fs/cgroup/cpu/cpu.stat'):\n"
            " if os.path.exists(p):\n"
            "  print(p);print(open(p).read());break\n"
            "else: print('cpu.stat not found')\n"
            "for p in ('/sys/fs/cgroup/cpu.max','/sys/fs/cgroup/cpu/cpu.cfs_quota_us'):\n"
            " if os.path.exists(p): print(p,open(p).read().strip())\n"
        )
        b64 = base64.b64encode(snippet.encode()).decode()
        return self._kubectl(["exec", self._name(pod, "pod"), "-n", self.ns, "--",
                              "python", "-c", f"import base64;exec(base64.b64decode('{b64}'))"], timeout=60)

    # ---- the only write tool ----
    def propose_fix(self, deployment: str, patch: dict, reason: str) -> str:
        name = self._name(deployment, "deployment")
        validate_patch(patch)
        if not isinstance(reason, str) or not reason.strip():
            raise ToolError("a reason with evidence is required")
        proposal = {"namespace": self.ns, "deployment": name, "patch": patch, "reason": reason.strip()}
        if not self._approve(proposal):
            return ("REJECTED by the human reviewer. Nothing was changed. Do not retry the same fix; "
                    "explain what you found and what you would recommend instead.")
        # Inline -p: args are a list (no shell) and the JSON comes from the already-validated dict.
        out = self._kubectl(["patch", "deployment", name, "-n", self.ns, "--type", "strategic",
                             "-p", json.dumps(patch)])
        rollout = self._kubectl(["rollout", "status", f"deployment/{name}", "-n", self.ns,
                                 "--timeout=120s"], timeout=150)
        return f"APPROVED and applied.\n{out}\n{rollout}\nNow verify the symptom with probe_latency / top_pods."

    # ---- dispatcher used by the agent loop ----
    def execute(self, tool: str, args: dict) -> str:
        fns = {"list_pods": self.list_pods, "describe": self.describe, "get_logs": self.get_logs,
               "top_pods": self.top_pods, "get_events": self.get_events,
               "probe_latency": self.probe_latency, "cpu_throttling": self.cpu_throttling,
               "propose_fix": self.propose_fix}
        if tool not in fns:
            raise ToolError(f"unknown tool '{tool}'")
        try:
            return fns[tool](**args)
        except TypeError as exc:
            raise ToolError(f"bad arguments for {tool}: {exc}")
