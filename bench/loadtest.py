#!/usr/bin/env python3
"""Load test harness for atlas-mcp. Not part of the test suite: pytest only collects `tests/`
(see pyproject.toml testpaths), and CI does not run this file.

Builds a synthetic org under a temp directory (small repos plus a few large monorepos), then
times the stages that do not require a Kiro CLI call: per-repo extraction, `generate` end to
end with a stubbed model call, `build()`'s graph join at increasing scale, and the MCP tool
functions serving the resulting graph. Everything real-money (the model call itself) is out of
scope: this measures the code around it, which is also the only work a free `restamp` run pays.

  uv run --group dev python bench/loadtest.py
  uv run --group dev python bench/loadtest.py --repos 20 --monorepos 1 --mono-files 2000 --stage A
"""

import argparse
import contextlib
import json
import os
import random
import sys
import time
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

import atlas

# Throwaway commits with a plain message must not run into a developer's global git config (a
# commit-msg hook, required signing) or a personal excludesFile that drops fixture files, the
# same reason tests/conftest.py isolates git for the test suite.
GIT_ISOLATION_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.excludesFile",
    "GIT_CONFIG_VALUE_0": os.devnull,
}


@contextlib.contextmanager
def isolated_git_env():
    saved = {k: os.environ.get(k) for k in GIT_ISOLATION_ENV}
    os.environ.update(GIT_ISOLATION_ENV)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------- synthetic repo generation ----------


def repo_files(name, index, n_files):
    """A repo shaped like the ones extract_facts and gather_bundle care about: CODEOWNERS, a
    k8s Service and Ingress, a compose file, a proto service, a Terraform queue, a go.mod, and
    enough source files carrying URL/env-var/datastore signal lines to exercise signal_sections."""
    files = {
        "README.md": f"# {name}\n\n{name} is a synthetic bench repo.\n",
        "CODEOWNERS": f"* @org/team-{index % 7}\n",
        "Dockerfile": "FROM python:3.11-slim\nCOPY . /app\n",
        "docker-compose.yml": f"services:\n  {name}:\n    build: .\n    image: {name}:latest\n",
        "k8s/service.yaml": (
            f"apiVersion: v1\nkind: Service\nmetadata:\n  name: {name}\n  namespace: prod\n"
            "spec:\n  ports:\n  - port: 80\n"
        ),
        "k8s/ingress.yaml": (
            "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n"
            f"  name: {name}\nspec:\n  rules:\n  - host: {name}.example.internal\n"
        ),
        "go.mod": (
            f"module github.com/bench-org/{name}\n\ngo 1.21\n\n"
            f"require github.com/bench-org/lib-{index % 11} v1.0.0\n"
        ),
        "proto/service.proto": (
            f'syntax = "proto3";\npackage {name.replace("-", "_")};\n\n'
            f"service {''.join(w.title() for w in name.split('-'))}Service {{\n}}\n"
        ),
        "infra/main.tf": (f'resource "aws_sqs_queue" "events" {{\n  name = "{name}-events"\n}}\n'),
    }
    filler_needed = max(0, n_files - len(files))
    for i in range(filler_needed):
        pkg = i % 25
        files[f"src/pkg{pkg}/file{i}.go"] = (
            f"package pkg{pkg}\n\n"
            f'const baseURL = "https://svc-{index % 11}.internal/api"\n'
            f'var target = os.Getenv("SVC_{index % 11}_SERVICE_URL")\n'
        )
    return files


def write_repo(root, name, files):
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    atlas.git(root, "init", "-q")
    atlas.git(root, "-c", "user.email=bench@example.com", "-c", "user.name=bench", "add", "-A")
    atlas.git(
        root,
        "-c",
        "user.email=bench@example.com",
        "-c",
        "user.name=bench",
        "commit",
        "-qm",
        "init",
    )


def build_org(src_root, n_repos, n_monorepos, mono_files):
    src_root.mkdir(parents=True, exist_ok=True)
    names = []
    for i in range(n_repos):
        name = f"svc-{i:04d}"
        write_repo(src_root / name, name, repo_files(name, i, n_files=150))
        names.append(name)
    for i in range(n_monorepos):
        name = f"mono-{i:02d}"
        print(f"  generating {name} ({mono_files} files)...", file=sys.stderr)
        write_repo(src_root / name, name, repo_files(name, i, n_files=mono_files))
        names.append(name)
    return names


# ---------- call counting ----------


class CallCounter:
    """Wraps a module-level function so callers that reference it unqualified (module-global
    lookup at call time, the same mechanism tests use to stub run_kiro) get counted."""

    def __init__(self, module, name):
        self.module, self.name, self.count = module, name, 0
        self._orig = getattr(module, name)

    def __enter__(self):
        counter = self

        def wrapped(*a, **k):
            counter.count += 1
            return counter._orig(*a, **k)

        setattr(self.module, self.name, wrapped)
        return self

    def __exit__(self, *exc):
        setattr(self.module, self.name, self._orig)


# ---------- stages ----------


def stage_a(repo_paths, label):
    print(f"\n## Stage A: per-repo extraction ({label})")
    print(
        f"{'repo':<12} {'files':>7} {'bundle_ms':>10} {'facts_ms':>9} {'pkgs_ms':>8} "
        f"{'git calls':>10} {'contained calls':>16}"
    )
    for repo in repo_paths:
        n_files = sum(1 for _ in repo.rglob("*") if _.is_file() and ".git" not in _.parts)
        with (
            CallCounter(atlas, "git") as git_calls,
            CallCounter(atlas, "contained") as contained_calls,
        ):
            t0 = time.perf_counter()
            atlas.gather_bundle(repo, atlas.DEFAULT_BUNDLE_BUDGET_BYTES)
            t1 = time.perf_counter()
            atlas.extract_facts(repo)
            t2 = time.perf_counter()
            atlas.extract_packages(repo)
            t3 = time.perf_counter()
        print(
            f"{repo.name:<12} {n_files:>7} {(t1 - t0) * 1000:>10.1f} {(t2 - t1) * 1000:>9.1f} "
            f"{(t3 - t2) * 1000:>8.1f} {git_calls.count:>10} {contained_calls.count:>16}"
        )


def stage_b(cfg, repo_paths, mapper_mode):
    print(f"\n## Stage B: generate end to end, stubbed Kiro call ({mapper_mode})")

    def fake_run_kiro(cfg, repo, prompt, log_path, mapper="explore"):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return {
            "summary": "s", "overview": "o", "domain": "unassigned", "kind": "service",
            "identifiers": [], "exposes": [], "consumes": [], "datastores": [],
        }  # fmt: skip

    args = argparse.Namespace(
        pull=False, full=True, dry_run=False, only=None, limit=None,
        no_build=True, force_unlock=False,
    )  # fmt: skip
    cfg = {**cfg, "mapper_mode": mapper_mode}
    orig_run_kiro = atlas.run_kiro
    orig_facts_version = atlas.FACTS_VERSION
    atlas.run_kiro = fake_run_kiro
    try:
        t0 = time.perf_counter()
        modes = {atlas.process_repo(cfg, name, path, args)[1] for name, path in repo_paths}
        elapsed = time.perf_counter() - t0
        print(
            f"{len(repo_paths)} repos, mode={'/'.join(sorted(modes))}: {elapsed:.2f}s total, "
            f"{elapsed / len(repo_paths) * 1000:.1f}ms/repo"
        )

        # Same commit, same prompt hash, but the facts stamp has moved: this is what actually
        # produces mode="restamp" (an unchanged HEAD still forces extract_facts/extract_packages
        # to rerun without spending a model call), not merely rerunning with --full unset, which
        # would resolve to the far cheaper "skip" path instead.
        args.full = False
        atlas.FACTS_VERSION = orig_facts_version + 1
        t0 = time.perf_counter()
        modes = {atlas.process_repo(cfg, name, path, args)[1] for name, path in repo_paths}
        elapsed = time.perf_counter() - t0
        print(
            f"{len(repo_paths)} repos, mode={'/'.join(sorted(modes))}: {elapsed:.2f}s total, "
            f"{elapsed / len(repo_paths) * 1000:.1f}ms/repo"
        )
    finally:
        atlas.run_kiro = orig_run_kiro
        atlas.FACTS_VERSION = orig_facts_version


def synth_manifest(name, n_repos, edges_per_repo, rng):
    peers = [f"svc-{rng.randrange(n_repos):04d}" for _ in range(edges_per_repo)]
    return {
        "name": name,
        "summary": f"{name} summary",
        "overview": "",
        "domain": "unassigned",
        "kind": "service",
        "languages": ["go"],
        "owners": ["team"],
        "identifiers": [name],
        "exposes": [
            {"kind": "http", "name": name, "key": name, "detail": "", "evidence": "README.md"}
        ],
        "consumes": [
            {
                "kind": "http",
                "name": p,
                "key": f"https://{p}.internal/api",
                "detail": "",
                "evidence": "README.md",
            }
            for p in peers
        ],
        "datastores": [],
        "components": [],
        "component_edges": [],
        "entrypoints": [],
        "notes": [],
        "packages": {"publishes": [], "depends_on": []},
        "_meta": {
            "repo_path": f"/bench/{name}",
            "commit": "0" * 40,
            "generated_at": "2026-01-01T00:00:00+00:00",
            "mode": "full",
            "model": "m",
            "last_full_at": "2026-01-01T00:00:00+00:00",
            "dropped_without_evidence": [],
        },
    }


def stage_c(atlas_dir, scale, edges_per_repo=10):
    print(f"\n## Stage C: build() at scale={scale}")
    rng = random.Random(scale)
    names = {f"svc-{i:04d}" for i in range(scale)}
    repos_dir = atlas_dir / "repos"
    repos_dir.mkdir(parents=True, exist_ok=True)
    for old in repos_dir.glob("*.json"):
        old.unlink()
    for name in names:
        atlas.write_json(
            repos_dir / f"{name}.json", synth_manifest(name, scale, edges_per_repo, rng)
        )
    config_path = atlas_dir / "config.json"
    config_path.write_text(json.dumps({"atlas_dir": str(atlas_dir)}))
    cfg = atlas.load_config(str(config_path))
    t0 = time.perf_counter()
    atlas.build(cfg, names)
    elapsed = time.perf_counter() - t0
    graph = json.loads((atlas_dir / "graph.json").read_text())
    print(f"{scale} repos, {len(graph['edges'])} edges: build() {elapsed:.3f}s")
    return atlas_dir


def percentiles(samples):
    s = sorted(samples)
    p50 = s[len(s) // 2]
    p95 = s[int(len(s) * 0.95)] if len(s) > 1 else s[0]
    return p50 * 1000, p95 * 1000


def stage_d(atlas_dir, scale):
    print(f"\n## Stage D: MCP tools at scale={scale}")
    try:
        import atlas_mcp
    except ImportError as e:
        print(f"  skipped: {e} (install the mcp package to run this stage)")
        return

    # atlas_mcp reads ATLAS_DIR once at import time, so a later scale must repoint the module's
    # ATLAS directly rather than the env var, and get a fresh Store to match.
    atlas_mcp.ATLAS = Path(atlas_dir)
    atlas_mcp.store = atlas_mcp.Store()
    graph = atlas_mcp.store.load()
    names = sorted(graph["repos"])
    rng = random.Random(scale)

    t0 = time.perf_counter()
    atlas_mcp.search("orders")
    cold_search = time.perf_counter() - t0

    calls = {
        "resolve": lambda: atlas_mcp.resolve(rng.choice(names)),
        "get_repo": lambda: atlas_mcp.get_repo(rng.choice(names)),
        "dependents": lambda: atlas_mcp.dependents(rng.choice(names)),
        "impact": lambda: atlas_mcp.impact(rng.choice(names), max_hops=3),
        "find_path": lambda: atlas_mcp.find_path(rng.choice(names), rng.choice(names)),
        "search": lambda: atlas_mcp.search("svc"),
    }
    print(f"{'tool':<12} {'p50_ms':>8} {'p95_ms':>8}")
    for label, fn in calls.items():
        samples = []
        for _ in range(200):
            t0 = time.perf_counter()
            fn()
            samples.append(time.perf_counter() - t0)
        p50, p95 = percentiles(samples)
        print(f"{label:<12} {p50:>8.2f} {p95:>8.2f}")

    t0 = time.perf_counter()
    atlas_mcp.freshness()
    freshness_all = time.perf_counter() - t0
    print(
        f"{'freshness()':<12} {freshness_all * 1000:>8.2f}  (no name, {len(names)} repos, cold search {cold_search * 1000:.2f}ms)"
    )


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repos", type=int, default=100)
    ap.add_argument("--monorepos", type=int, default=2)
    ap.add_argument("--mono-files", type=int, default=20000)
    ap.add_argument("--graph-scales", default="100,1000")
    ap.add_argument("--stages", default="A,B,C,D", help="comma-separated subset of A,B,C,D")
    ap.add_argument(
        "--keep", action="store_true", help="print the temp dir instead of cleaning it up"
    )
    args = ap.parse_args()
    stages = set(args.stages.split(","))

    import tempfile

    tmp = tempfile.mkdtemp(prefix="atlas-bench-")
    tmp_path = Path(tmp)
    try:
        with isolated_git_env():
            src_root = tmp_path / "src"
            atlas_dir = tmp_path / "atlas"
            print(
                f"generating {args.repos} small repos + {args.monorepos} monorepos under {src_root}"
            )
            names = build_org(src_root, args.repos, args.monorepos, args.mono_files)
            repo_paths = [src_root / n for n in names]

            if "A" in stages:
                small = [p for p in repo_paths if p.name.startswith("svc-")][:10]
                mono = [p for p in repo_paths if p.name.startswith("mono-")]
                if small:
                    stage_a(small, "small repos, first 10")
                if mono:
                    stage_a(mono, "monorepos")

            if "B" in stages:
                config_path = tmp_path / "config.json"
                config_path.write_text(json.dumps({"atlas_dir": str(atlas_dir)}))
                cfg = atlas.load_config(str(config_path))
                small_named = [(p.name, p) for p in repo_paths if p.name.startswith("svc-")]
                stage_b(cfg, small_named, "bundle")

            if "C" in stages or "D" in stages:
                for scale in (int(s) for s in args.graph_scales.split(",")):
                    build_atlas_dir = tmp_path / f"atlas-{scale}"
                    if "C" in stages:
                        stage_c(build_atlas_dir, scale)
                    if "D" in stages and (build_atlas_dir / "graph.json").exists():
                        stage_d(build_atlas_dir, scale)
    finally:
        if args.keep:
            print(f"\nkept bench tree at {tmp_path}")
        else:
            import shutil

            shutil.rmtree(tmp_path, ignore_errors=True)


if __name__ == "__main__":
    main()
