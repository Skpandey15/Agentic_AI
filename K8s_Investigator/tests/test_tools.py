import json

import pytest

from investigator.tools import KubeTools, ToolError, clip, validate_patch

GOOD_PATCH = {"spec": {"template": {"spec": {"containers": [
    {"name": "app", "resources": {"limits": {"cpu": "500m"}, "requests": {"cpu": "100m"}}}]}}}}


class FakeRunner:
    def __init__(self, stdout="ok", code=0, stderr=""):
        self.calls, self.stdout, self.code, self.stderr = [], stdout, code, stderr

    def __call__(self, cmd, stdin, timeout):
        self.calls.append({"cmd": cmd, "stdin": stdin})
        return self.code, self.stdout, self.stderr

    def verbs(self):
        return [c["cmd"][3] for c in self.calls]  # ["wsl","-e","kubectl", <verb>, ...]


def tools(approve=True, runner=None, ns="sandbox"):
    r = runner or FakeRunner()
    return KubeTools(ns, lambda p: approve, runner=r), r


# ---- namespace guard: the live app must be unreachable ----
@pytest.mark.parametrize("ns", ["default", "kube-system", "", "sandboxx", "sandbox2", "Sandbox", "sandbox-",
                                "sandbox_x", "sandbox-a b", "my-sandbox", "sandbox/../default", None])
def test_namespace_allowlist(ns):
    with pytest.raises(ValueError):
        tools(ns=ns)


@pytest.mark.parametrize("ns", ["sandbox", "sandbox-ev-cpu-1", "sandbox-a"])
def test_sandbox_namespaces_allowed(ns):
    assert tools(ns=ns)[0].ns == ns


# ---- argument validation: no shell metacharacters reach kubectl ----
@pytest.mark.parametrize("bad", ["pod; rm -rf /", "../etc", "A_B", "x y", "$(id)", "", "a" * 70, None, 5])
def test_bad_names_rejected_without_running_kubectl(bad):
    t, r = tools()
    with pytest.raises(ToolError):
        t.get_logs(bad)
    assert r.calls == []


def test_describe_kind_allowlist():
    t, r = tools()
    with pytest.raises(ToolError):
        t.describe("secret", "weather-agent-secrets")  # secrets must be unreadable
    assert r.calls == []


def test_describe_ok_targets_namespace():
    t, r = tools()
    t.describe("pod", "report-api-abc")
    cmd = r.calls[0]["cmd"]
    assert cmd[:4] == ["wsl", "-e", "kubectl", "describe"] and cmd[-2:] == ["-n", "sandbox"]


@pytest.mark.parametrize("tail,expected", [(1000, "--tail=200"), (0, "--tail=1"), (-5, "--tail=1"), (50, "--tail=50")])
def test_logs_tail_clamped(tail, expected):
    t, r = tools()
    t.get_logs("report-api-abc", tail=tail)
    assert expected in r.calls[0]["cmd"]


@pytest.mark.parametrize("bad", ["health", "/a?x=1", "/a b", "/../x;ls", "/" + "a" * 70])
def test_probe_path_validation(bad):
    t, r = tools()
    with pytest.raises(ToolError):
        t.probe_latency("report-api-abc", bad)
    assert r.calls == []


def test_probe_count_clamped_and_runs_in_namespace():
    t, r = tools(runner=FakeRunner(stdout='{"ok":1}'))
    t.probe_latency("report-api-abc", "/report", count=999)
    cmd = r.calls[0]["cmd"]
    assert cmd[3] == "exec" and "-n" in cmd and cmd[cmd.index("-n") + 1] == "sandbox"


def test_kubectl_failure_becomes_tool_error_not_crash():
    t, _ = tools(runner=FakeRunner(code=1, stderr="Error from server (NotFound)"))
    with pytest.raises(ToolError, match="NotFound"):
        t.list_pods()


def test_unknown_tool_and_bad_args():
    t, _ = tools()
    with pytest.raises(ToolError):
        t.execute("delete_namespace", {})
    with pytest.raises(ToolError):
        t.execute("get_logs", {"nonsense": 1})


def test_clip_truncates_long_output():
    out = clip("x" * 20000)
    assert len(out) < 7000 and "truncated" in out


# ---- patch allowlist ----
def test_good_patch_accepted():
    assert validate_patch(GOOD_PATCH) is GOOD_PATCH
    validate_patch({"spec": {"replicas": 3}})


@pytest.mark.parametrize("patch", [
    "string", None, {}, {"metadata": {"name": "x"}}, {"spec": {"replicas": 0}}, {"spec": {"replicas": 99}},
    {"spec": {"replicas": "3"}}, {"spec": {"selector": {}}},
    {"spec": {"template": {"spec": {"containers": [{"name": "app", "image": "evil:latest"}]}}}},
    {"spec": {"template": {"spec": {"containers": [{"name": "app", "command": ["sh"]}]}}}},
    {"spec": {"template": {"spec": {"containers": [{"name": "app", "securityContext": {"privileged": True}}]}}}},
    {"spec": {"template": {"spec": {"hostNetwork": True, "containers": []}}}},
    {"spec": {"template": {"spec": {"volumes": [], "containers": []}}}},
    {"spec": {"template": {"spec": {"containers": [{"resources": {"limits": {"cpu": "1"}}}]}}}},
    {"spec": {"template": {"spec": {"containers": [{"name": "app", "resources": {"limits": {"gpu": "1"}}}]}}}},
    {"spec": {"template": {"spec": {"containers": [{"name": "app", "env": [{"name": "A", "valueFrom": {}}]}]}}}},
])
def test_dangerous_or_malformed_patches_rejected(patch):
    with pytest.raises(ToolError, match="rejected"):
        validate_patch(patch)


# ---- the human gate ----
def test_rejected_fix_never_runs_a_write():
    t, r = tools(approve=False)
    out = t.propose_fix("report-api", GOOD_PATCH, "cpu pinned at limit")
    assert "REJECTED" in out and r.calls == []


def test_invalid_patch_never_reaches_approver_or_kubectl():
    asked = []
    r = FakeRunner()
    t = KubeTools("sandbox", lambda p: asked.append(p) or True, runner=r)
    with pytest.raises(ToolError):
        t.propose_fix("report-api", {"spec": {"template": {"spec": {"containers": [{"name": "a", "image": "x"}]}}}}, "r")
    assert asked == [] and r.calls == []


def test_reason_required():
    t, r = tools()
    with pytest.raises(ToolError):
        t.propose_fix("report-api", GOOD_PATCH, "   ")
    assert r.calls == []


def test_approved_fix_patches_inline_then_waits_for_rollout():
    t, r = tools(approve=True)
    out = t.propose_fix("report-api", GOOD_PATCH, "cpu 20m/20m throttled")
    assert r.verbs() == ["patch", "rollout"] and "APPROVED" in out
    patch_call = r.calls[0]
    assert "--patch-file" not in patch_call["cmd"] and patch_call["stdin"] is None
    assert json.loads(patch_call["cmd"][patch_call["cmd"].index("-p") + 1]) == GOOD_PATCH
    assert patch_call["cmd"][patch_call["cmd"].index("-n") + 1] == "sandbox"


def test_approver_sees_full_proposal():
    seen = []
    t = KubeTools("sandbox", lambda p: seen.append(p) or False, runner=FakeRunner())
    t.propose_fix("report-api", GOOD_PATCH, "evidence here")
    assert seen[0]["deployment"] == "report-api" and seen[0]["patch"] == GOOD_PATCH
    assert seen[0]["namespace"] == "sandbox" and seen[0]["reason"] == "evidence here"


def test_cpu_throttling_is_fixed_readonly_exec_in_namespace():
    t, r = tools(runner=FakeRunner(stdout="nr_throttled 5"))
    assert "nr_throttled" in t.cpu_throttling("report-api-abc")
    cmd = r.calls[0]["cmd"]
    assert cmd[3] == "exec" and cmd[cmd.index("-n") + 1] == "sandbox"


def test_cpu_throttling_validates_pod_name():
    t, r = tools()
    with pytest.raises(ToolError):
        t.cpu_throttling("x; reboot")
    assert r.calls == []
