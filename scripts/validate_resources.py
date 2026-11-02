#!/usr/bin/env python3
"""Validate repo-authored Kubernetes resource policy without contacting a cluster."""

import argparse
from decimal import Decimal
from pathlib import Path
import re
import subprocess
import sys

import yaml


ROOTS = ("application", "infrastructure", "clusters")
POD_PATHS = {
    "Pod": ("spec",),
    "Deployment": ("spec", "template", "spec"),
    "StatefulSet": ("spec", "template", "spec"),
    "DaemonSet": ("spec", "template", "spec"),
    "ReplicaSet": ("spec", "template", "spec"),
    "ReplicationController": ("spec", "template", "spec"),
    "Job": ("spec", "template", "spec"),
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
    "ScaledJob": ("spec", "jobTargetRef", "template", "spec"),
}
QUANTITY = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))([eE][+-]?\d+|[numkKMGTPE]|[KMGTPE]i)?")
FACTORS = {"": Decimal(1), "n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3")}
FACTORS.update({s: Decimal(1000) ** i for i, s in enumerate("kMGTPE", 1)})
FACTORS.update({s + "i": Decimal(1024) ** i for i, s in enumerate("KMGTPE", 1)})


def quantity(value):
    match = QUANTITY.fullmatch(str(value))
    if not match:
        raise ValueError("invalid quantity")
    number, suffix = match.groups()
    suffix = suffix or ""
    factor = Decimal(10) ** int(suffix[1:]) if len(suffix) > 1 and suffix[0] in "eE" and not suffix.endswith("i") else FACTORS.get(suffix)
    if factor is None:
        raise ValueError("invalid quantity suffix")
    return Decimal(number) * factor


def check_resources(resources, location, errors, deletion_patch=False):
    if not isinstance(resources, dict):
        errors.append(f"{location}: resources must be a mapping")
        return
    if deletion_patch and resources == {"limits": {"cpu": None}}:
        return
    requests = resources.get("requests") or {}
    limits = resources.get("limits") or {}
    if not isinstance(requests, dict) or not isinstance(limits, dict):
        errors.append(f"{location}: requests and limits must be mappings")
        return
    if limits.get("cpu") is not None:
        errors.append(f"{location}: CPU limits are forbidden; use requests.cpu only")
    values = {}
    for field, value in (("requests.cpu", requests.get("cpu")), ("requests.memory", requests.get("memory")), ("limits.memory", limits.get("memory"))):
        if value is None:
            errors.append(f"{location}: {field} is required")
            continue
        try:
            values[field] = quantity(value)
            if values[field] <= 0:
                raise ValueError("non-positive quantity")
        except ValueError:
            errors.append(f"{location}: {field} must be a positive Kubernetes quantity")
    if "requests.memory" in values and "limits.memory" in values and values["requests.memory"] != values["limits.memory"]:
        errors.append(f"{location}: requests.memory must equal limits.memory")


def check_values(value, location, errors, resource_tree=False):
    """Handle both resources: {requests, limits} and KEDA's resources: {operator, ...}."""
    if isinstance(value, dict):
        if resource_tree and (not value or "requests" in value or "limits" in value):
            check_resources(value, location, errors)
            return
        for key, child in value.items():
            check_values(child, f"{location}.{key}", errors, resource_tree or key == "resources")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            check_values(child, f"{location}[{index}]", errors, resource_tree)
    elif resource_tree:
        check_resources(value, location, errors)


def container_json_patch(operation):
    ancestors = {""}
    for path in POD_PATHS.values():
        ancestors.update("/" + "/".join(path[:i]) for i in range(1, len(path) + 1))
    container_keys = {"resources", "containers", "initContainers", "ephemeralContainers"}
    for key in ("path", "from"):
        if key in operation:
            path = str(operation[key])
            if path in ancestors or container_keys & set(path.split("/")):
                return True

    def has_container_keys(value):
        if isinstance(value, dict):
            return bool(container_keys & value.keys()) or any(has_container_keys(child) for child in value.values())
        if isinstance(value, list):
            return any(has_container_keys(child) for child in value)
        return False

    return has_container_keys(operation.get("value"))


def has_merge_directives(value):
    if isinstance(value, dict):
        return any(str(key).startswith("$") for key in value) or any(has_merge_directives(child) for child in value.values())
    if isinstance(value, list):
        return any(has_merge_directives(child) for child in value)
    return False


def check_document(doc, source, errors, patch=False):
    if not isinstance(doc, dict):
        return
    kind = doc.get("kind")
    name = doc.get("metadata", {}).get("name", "unnamed")
    location = f"{source}: {kind}/{name}"
    if kind == "List":
        for child in doc.get("items", []):
            check_document(child, source, errors, patch)
    elif kind in POD_PATHS:
        if patch and has_merge_directives(doc):
            errors.append(f"{location}: strategic-merge directives are unsupported; use plain container resource mappings")
            return
        pod = doc
        for key in POD_PATHS[kind]:
            pod = pod.get(key, {})
        for group in ("containers", "initContainers", "ephemeralContainers"):
            for container in pod.get(group, []):
                if patch and "resources" not in container:
                    continue
                check_resources(container.get("resources", {}), f"{location}.{group}[{container.get('name', '?')}]", errors, deletion_patch=patch)
    elif kind == "HelmRelease":
        spec = doc.get("spec", {})
        check_values(spec.get("values", {}), location + ".values", errors)
        for renderer in spec.get("postRenderers", []):
            for entry in renderer.get("kustomize", {}).get("patches", []):
                for body in load_yaml(entry.get("patch", ""), location + ".postRenderers", errors):
                    if isinstance(body, list):
                        # A JSON patch cannot be checked as a complete resource block.
                        if any(container_json_patch(op) for op in body):
                            errors.append(f"{location}: use a strategic-merge patch for containers, not JSON patch")
                    else:
                        check_document(body, location + ".postRenderers", errors, patch=True)
    elif kind is None:
        check_values(doc, source, errors)


def load_yaml(text, source, errors):
    try:
        return list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = f" at line {mark.line + 1}" if mark else ""
        errors.append(f"{source}: invalid YAML{line}")
        return []


def local_kustomize_inputs(path, root, errors, seen):
    """Reject remote/out-of-repository inputs before Kustomize can fetch anything."""
    path = path.resolve()
    if path in seen:
        return set()
    seen.add(path)
    source = str(path.relative_to(root))
    docs = load_yaml(path.read_text(), source, errors)
    inputs = set()
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") not in ("Kustomization", "Component"):
            continue
        refs = []
        for key in ("resources", "bases", "components", "configurations", "crds", "generators", "transformers"):
            refs.extend(doc.get(key, []))
        for key in ("patches", "patchesJson6902", "patchesStrategicMerge"):
            for entry in doc.get(key, []):
                if isinstance(entry, str):
                    # Legacy strategic merge patches may be inline YAML.
                    if "\n" not in entry:
                        refs.append(entry)
                elif isinstance(entry, dict) and "path" in entry:
                    refs.append(entry["path"])
        if "path" in doc.get("openapi", {}):
            refs.append(doc["openapi"]["path"])
        for key in ("configMapGenerator", "secretGenerator"):
            for generator in doc.get(key, []):
                refs.extend(value.split("=", 1)[-1] for value in generator.get("files", []))
                refs.extend(generator.get("envs", []))
        for ref in refs:
            candidate = (path.parent / ref).resolve()
            if not candidate.is_relative_to(root) or not candidate.exists():
                errors.append(f"{source}: Kustomize inputs must be existing local paths inside this repository")
                continue
            inputs.add(candidate)
            if candidate.is_dir():
                for name in ("kustomization.yaml", "kustomization.yml", "Kustomization"):
                    nested = candidate / name
                    if nested.is_file():
                        inputs.add(nested)
                        inputs.update(local_kustomize_inputs(nested, root, errors, seen))
                        break
            elif candidate.suffix in (".yaml", ".yml"):
                inputs.update(local_kustomize_inputs(candidate, root, errors, seen))
    return inputs


def validate_repository(root):
    errors = []
    documents = {}
    for directory in ROOTS:
        for path in sorted((root / directory).rglob("*")):
            if (path.suffix in (".yaml", ".yml") or path.name == "Kustomization") and not path.name.startswith("."):
                source = str(path.relative_to(root))
                documents[path] = load_yaml(path.read_text(), source, errors)

    # Kustomize supplies actual merge semantics, so partial Flux patches are not
    # mistaken for complete workloads and overridden base defaults are not checked.
    kustomizations = [path for path, docs in documents.items() if any(
        isinstance(doc, dict) and doc.get("kind") == "Kustomization" and
        str(doc.get("apiVersion", "")).startswith("kustomize.config.k8s.io/")
        for doc in docs
    )]
    consumed = set()
    seen = set()
    for path in kustomizations:
        consumed.update(local_kustomize_inputs(path, root, errors, seen))
    if errors:
        return errors
    builds = [path for path in kustomizations if path not in consumed]
    if kustomizations and not builds:
        return ["Kustomize inputs form a cycle; no root Kustomization can be validated"]
    consumed.update(kustomizations)
    for path in builds:
        source = str(path.parent.relative_to(root)) + " (kustomize)"
        try:
            result = subprocess.run(["kubectl", "kustomize", str(path.parent)], capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return errors + ["kubectl is required for offline Kustomize rendering; install it and retry"]
        if result.returncode:
            errors.append(f"{source}: kubectl kustomize failed\n{result.stderr.strip()}")
            continue
        for rendered in load_yaml(result.stdout, source, errors):
            check_document(rendered, source, errors)
    for path, docs in documents.items():
        for doc in docs:
            # Generator input values are not represented as workload resources
            # in rendered ConfigMaps, so check their source YAML as well.
            if path not in consumed or isinstance(doc, dict) and doc.get("kind") is None:
                check_document(doc, str(path.relative_to(root)), errors)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    errors = validate_repository(args.root.resolve())
    if errors:
        print("Resource policy failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print(f"{len(errors)} violation(s)", file=sys.stderr)
        return 1
    print("Resource policy passed (repo-authored manifests and Helm resource blocks).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
