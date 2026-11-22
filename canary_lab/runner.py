"""Create an isolated kind cluster and verify the real deployment outcomes."""

import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4

NODE_IMAGE = "kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
ARGO_MANIFEST = "https://github.com/argoproj/argo-rollouts/releases/download/v1.10.2/install.yaml"
ROOT = Path(__file__).resolve().parent.parent
NAMESPACE = "canary-lab"
FINISHED = {"Successful", "Failed", "Error", "Inconclusive"}


class LabError(RuntimeError):
    pass


def command(args, *, timeout=60, input=None) -> str:
    try:
        result = subprocess.run(args, cwd=ROOT, input=input, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, check=True)
    except subprocess.CalledProcessError as error:
        detail = "\n".join(filter(None, (error.stdout, error.stderr))).strip()[-3500:]
        raise LabError(f"{' '.join(args[:4])} failed ({error.returncode})\n{detail}") from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LabError(f"{' '.join(args[:4])}: {error}") from error
    return result.stdout


def wait_for(description, predicate, timeout=90):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        result = predicate()
        if result:
            return result
        sleep(1)
    raise LabError(f"Timed out waiting for {description}.")


def traffic_delta(before: list[dict], after: list[dict]) -> dict:
    def index(rows):
        return {(row["release"], row["revision"], row["outcome"]): row["requests"] for row in rows}

    initial, final = index(before), index(after)
    if any(final.get(key, 0) < count for key, count in initial.items()):
        raise LabError("Traffic generator counters reset during the measurement.")
    counters = {}
    for key, count in final.items():
        released, _, outcome = key
        delta = count - initial.get(key, 0)
        if delta:
            counters.setdefault(released, Counter())[outcome] += delta
    return {release: {"requests": sum(counts.values()), "errors": counts["error"]}
            for release, counts in counters.items()}


class Cluster:
    def __init__(self, directory: Path):
        tag = uuid4().hex[:10]
        self.name = f"canary-lab-{tag}"
        self.context = f"kind-{self.name}"
        self.kubeconfig = str(directory / "config")
        self.image = f"canary-release-lab:{tag}"

    def kube(self, *args, namespace=NAMESPACE, timeout=45, input=None) -> str:
        return command(["kubectl", "--kubeconfig", self.kubeconfig, "--context", self.context,
                        "-n", namespace, *args], timeout=timeout, input=input)

    def get(self, *args, namespace=NAMESPACE) -> dict:
        return json.loads(self.kube("get", *args, "-o", "json", namespace=namespace))

    def prepare(self) -> None:
        print("Creating a disposable kind cluster.", flush=True)
        command(["kind", "create", "cluster", "--name", self.name, "--kubeconfig", self.kubeconfig,
                 "--image", NODE_IMAGE, "--wait", "120s"], timeout=300)
        self.kube("create", "namespace", "argo-rollouts")
        self.kube("create", "namespace", NAMESPACE)
        print("Installing Argo Rollouts and building the checkout image.", flush=True)
        self.kube("apply", "--server-side", "-f", ARGO_MANIFEST, namespace="argo-rollouts", timeout=120)
        command(["docker", "build", "-t", self.image, "."], timeout=240)
        command(["kind", "load", "docker-image", self.image, "--name", self.name], timeout=160)
        self.kube("rollout", "status", "deployment/argo-rollouts", "--timeout=120s",
                  namespace="argo-rollouts", timeout=140)
        for filename in ("checkout.yaml", "monitoring.yaml"):
            manifest = (ROOT / "kubernetes" / filename).read_text(encoding="utf-8")
            self.kube("apply", "-f", "-", input=manifest.replace("canary-release-lab:local", self.image))
        for deployment in ("prometheus", "traffic-generator"):
            self.kube("rollout", "status", f"deployment/{deployment}", "--timeout=120s", timeout=140)

    def deploy(self, release: str, failure_interval: int) -> None:
        patch = {"spec": {"template": {"metadata": {"labels": {"release": release},
                                                   "annotations": {"lab.example/failure-interval": str(failure_interval)}}}}}
        self.kube("patch", "rollout", "checkout", "--type=merge", "-p", json.dumps(patch, separators=(",", ":")))

    def snapshot(self) -> list[dict]:
        return json.loads(self.kube("exec", "deployment/traffic-generator", "--",
                                    "python", "-m", "canary_lab.app", "snapshot", timeout=15))

    def phase(self) -> dict:
        return self.get("rollout", "checkout").get("status", {})

    def analysis(self) -> list[dict]:
        return self.get("analysisruns").get("items", [])

    def resources(self, output: Path) -> None:
        reports = {
            "rollout.json": ("get", "rollout", "checkout", "-o", "json"),
            "analysis-runs.json": ("get", "analysisruns", "-o", "json"),
            "pods.txt": ("get", "pods", "-o", "wide"),
            "events.txt": ("get", "events", "--sort-by=.metadata.creationTimestamp"),
            "prometheus.log": ("logs", "deployment/prometheus", "--tail=60"),
        }
        for name, args in reports.items():
            try:
                (output / name).write_text(self.kube(*args, timeout=8), encoding="utf-8")
            except (LabError, OSError):
                pass
        try:
            (output / "controller.log").write_text(
                self.kube("logs", "deployment/argo-rollouts", "--tail=80", namespace="argo-rollouts", timeout=8),
                encoding="utf-8",
            )
        except (LabError, OSError):
            pass

    def remove(self) -> None:
        command(["kind", "delete", "cluster", "--name", self.name, "--kubeconfig", self.kubeconfig], timeout=90)
        try:
            command(["docker", "image", "rm", self.image], timeout=45)
        except LabError:
            pass


def stable(cluster: Cluster) -> str | bool:
    state = cluster.phase()
    current = state.get("currentPodHash")
    if (current and state.get("stableRS") == current and state.get("readyReplicas", 0) >= 4
            and state.get("phase") == "Healthy"):
        return current
    return False


def sample_stable(cluster: Cluster, expected: str, limit: int = 45) -> dict:
    def measure():
        previous = cluster.snapshot()
        sleep(3)
        sample = traffic_delta(previous, cluster.snapshot())
        info = sample.get(expected, {})
        if set(sample) == {expected} and info["requests"] >= 50 and info["errors"] == 0:
            return sample
        return None

    return wait_for(f"50 or more consecutive successful requests on {expected}", measure, timeout=limit)


def release_check(cluster: Cluster, release: str, faults: int, expected: str) -> tuple[dict, float]:
    known_runs = {item["metadata"]["name"] for item in cluster.analysis()}
    previous = cluster.snapshot()
    cluster.deploy(release, faults)

    def on_route():
        snapshot = cluster.snapshot()
        seen = traffic_delta(previous, snapshot).get(release, {})
        return snapshot if seen.get("requests", 0) >= 5 else None

    live = wait_for(f"{release} to receive shared-service traffic", on_route)

    def completed_run():
        for item in cluster.analysis():
            if item["metadata"]["name"] not in known_runs and item.get("status", {}).get("phase") in FINISHED:
                return item
        return None

    run = wait_for(f"{release} to finish Argo analysis", completed_run)
    finished = monotonic()
    summary = run["status"]
    if summary["phase"] != expected:
        raise LabError(f"{release} analysis ended {summary['phase']}, expected {expected}. {summary.get('message', '')}")
    sampled = traffic_delta(live, cluster.snapshot())
    candidate = sampled.get(release, {})
    total = sum(row["requests"] for row in sampled.values())
    if not candidate.get("requests") or not total or candidate["requests"] >= total:
        raise LabError(f"{release} did not share traffic with the stable release during analysis.")
    metrics = {
        metric["name"]: {
            "phase": metric.get("phase"),
            "measurements": [{"phase": row.get("phase"), "value": row.get("value")} for row in metric.get("measurements", [])],
        }
        for metric in summary.get("metricResults", [])
    }
    rate = candidate["errors"] / candidate["requests"]
    if expected == "Successful" and rate > 0.03:
        raise LabError("Healthy release exceeded the error-rate threshold.")
    if expected == "Failed" and (rate <= 0.03 or metrics.get("canary-5xx-rate", {}).get("phase") != "Failed"):
        raise LabError("The failing release was not rejected by the candidate-specific 5xx check.")
    return {
        "release": release,
        "analysis_run": run["metadata"]["name"],
        "analysis_phase": summary["phase"],
        "metrics": metrics,
        "during_canary": sampled,
        "observed_candidate_fraction": round(candidate["requests"] / total, 4),
        "candidate_5xx_fraction": round(rate, 4),
    }, finished


def prove(cluster: Cluster) -> dict:
    wait_for("the initial stable rollout", lambda: stable(cluster))
    baseline = sample_stable(cluster, "v1")
    print("Deploying healthy v2 and waiting for promotion.", flush=True)
    healthy, _ = release_check(cluster, "v2", 0, "Successful")
    stable_hash = wait_for("v2 promotion", lambda: stable(cluster))
    healthy["after_promotion"] = sample_stable(cluster, "v2")
    print("Deploying faulty v3 and waiting for automatic abort.", flush=True)
    faulty, failed_at = release_check(cluster, "v3", 3, "Failed")
    faulty["after_abort"] = sample_stable(cluster, "v2")
    faulty["seconds_from_observed_failed_analysis_to_stable_sample"] = round(monotonic() - failed_at, 2)
    status = cluster.phase()
    if status.get("stableRS") != stable_hash or status.get("stableRS") == status.get("currentPodHash"):
        raise LabError("Argo did not retain the last promoted revision after the failed candidate.")
    faulty["stable_revision_retained"] = stable_hash
    return {"baseline": baseline, "healthy": healthy, "faulty": faulty}


def run(output: Path) -> dict:
    for executable in ("docker", "kind", "kubectl"):
        if not shutil.which(executable):
            raise LabError(f"{executable} is required on PATH.")
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "report.json"
    if report_path.exists():
        raise LabError("Choose a new output directory to keep this report separate from previous runs.")
    command(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=12)
    with TemporaryDirectory(prefix="canary-lab-") as directory:
        cluster = Cluster(Path(directory))
        try:
            cluster.prepare()
            result = prove(cluster)
            report_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return result
        finally:
            cluster.resources(output)
            cluster.remove()
