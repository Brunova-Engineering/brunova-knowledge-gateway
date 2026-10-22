"""Verify an image-only Cloud Run revision without printing service settings."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def read_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def container_config(service: dict[str, Any]) -> list[dict[str, Any]]:
    containers = service["spec"]["template"]["spec"]["containers"]
    return [{key: value for key, value in container.items() if key != "image"} for container in containers]


def traffic_without_release_tag(service: dict[str, Any], release_tag: str) -> list[dict[str, Any]]:
    entries = [
        entry for entry in service.get("status", {}).get("traffic", [])
        if entry.get("tag") != release_tag
    ]
    return sorted(entries, key=lambda entry: (entry.get("tag", ""), entry.get("revisionName", "")))


def active_traffic(service: dict[str, Any]) -> list[dict[str, Any]]:
    """Ignore untagged zero-percent revision entries, which carry no traffic."""
    entries = [
        entry
        for entry in service.get("status", {}).get("traffic", [])
        if entry.get("tag") or entry.get("percent", 0) > 0
    ]
    return sorted(entries, key=lambda entry: (entry.get("tag", ""), entry.get("revisionName", "")))


def ready(revision: dict[str, Any]) -> bool:
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in revision.get("status", {}).get("conditions", [])
    )


def resolved_image(commit_image: str, digest: str) -> str:
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise SystemExit("Artifact Registry did not return a valid image digest.")
    repository, separator, tag = commit_image.rpartition(":")
    if not separator or not repository or not re.fullmatch(r"[a-f0-9]{40}", tag):
        raise SystemExit("Expected image must carry the exact commit SHA tag.")
    return f"{repository}@{digest}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before")
    parser.add_argument("--after")
    parser.add_argument("--revision")
    parser.add_argument("--image")
    parser.add_argument("--resolved-digest")
    parser.add_argument("--tag")
    parser.add_argument("--expected-revision")
    parser.add_argument("--github-output")
    parser.add_argument("--verify-traffic-restored", action="store_true")
    parser.add_argument("--verify-promoted-revision")
    parser.add_argument("--traffic-targets")
    parser.add_argument("--ignore-tag")
    args = parser.parse_args()

    if args.traffic_targets:
        service = read_json(args.traffic_targets)
        routes = [
            entry for entry in service.get("status", {}).get("traffic", [])
            if not entry.get("tag") and entry.get("percent", 0) > 0
        ]
        if not routes or any(not entry.get("revisionName") for entry in routes):
            raise SystemExit("Cannot safely derive the prior production revision assignments for rollback.")
        if sum(entry.get("percent", 0) for entry in routes) != 100:
            raise SystemExit("Prior production traffic percentages do not total 100; refusing unsafe rollback.")
        print(",".join(f"{entry['revisionName']}={entry['percent']}" for entry in routes))
        return 0

    if args.before and args.after:
        before, after = read_json(args.before), read_json(args.after)
        if args.verify_traffic_restored:
            restored = [entry for entry in active_traffic(after) if entry.get("tag") != args.ignore_tag]
            if active_traffic(before) != restored:
                raise SystemExit("Cloud Run traffic assignments were not restored after verification.")
            print("Verified original Cloud Run traffic assignments are restored.")
            return 0

        if args.verify_promoted_revision:
            expected = [{"revisionName": args.verify_promoted_revision, "percent": 100}]
            promoted = [entry for entry in active_traffic(after) if not entry.get("tag")]
            if promoted != expected:
                raise SystemExit("Production traffic is not assigned 100% to the verified revision.")
            before_tags = [
                entry for entry in before.get("status", {}).get("traffic", [])
                if entry.get("tag") and entry.get("tag") != args.ignore_tag
            ]
            after_tags = [
                entry for entry in after.get("status", {}).get("traffic", [])
                if entry.get("tag") and entry.get("tag") != args.ignore_tag
            ]
            if sorted(before_tags, key=lambda entry: entry["tag"]) != sorted(after_tags, key=lambda entry: entry["tag"]):
                raise SystemExit("Existing tagged Cloud Run routes changed during promotion.")
            print("Verified 100% production traffic on the verified revision and preserved tagged routes.")
            return 0

        if not args.image or not args.tag:
            raise SystemExit("--image and --tag are required for a revision deployment check.")
        before_template = before["spec"]["template"]["spec"]
        after_template = after["spec"]["template"]["spec"]
        if before.get("metadata", {}).get("name") != after.get("metadata", {}).get("name"):
            raise SystemExit("Cloud Run service name changed during deployment.")
        if before_template.get("serviceAccountName") != after_template.get("serviceAccountName"):
            raise SystemExit("Cloud Run runtime service account changed during deployment.")
        if container_config(before) != container_config(after):
            raise SystemExit("Cloud Run container settings changed beyond the image.")
        before_template_without_containers = {
            key: value for key, value in before_template.items() if key != "containers"
        }
        after_template_without_containers = {
            key: value for key, value in after_template.items() if key != "containers"
        }
        if before_template_without_containers != after_template_without_containers:
            raise SystemExit("Cloud Run revision settings changed during deployment.")
        if traffic_without_release_tag(before, args.tag) != traffic_without_release_tag(after, args.tag):
            raise SystemExit("Existing Cloud Run traffic assignments changed during deployment.")

        revision_name = after.get("status", {}).get("latestCreatedRevisionName")
        if not revision_name or after.get("status", {}).get("latestReadyRevisionName") != revision_name:
            raise SystemExit("The newly created Cloud Run revision is not ready.")
        containers = after_template.get("containers", [])
        if not containers or containers[0].get("image") != args.image:
            raise SystemExit("Cloud Run service does not reference the commit-tagged image.")
        release_route = next(
            (entry for entry in after.get("status", {}).get("traffic", []) if entry.get("tag") == args.tag),
            None,
        )
        if not release_route or release_route.get("revisionName") != revision_name or not release_route.get("url"):
            raise SystemExit("Cloud Run did not create the expected tagged revision route.")
        if args.github_output:
            with Path(args.github_output).open("a", encoding="utf-8") as output:
                output.write(f"revision={revision_name}\n")
                output.write(f"url={release_route['url']}\n")
                service_url = after.get("status", {}).get("url")
                if not service_url:
                    raise SystemExit("Cloud Run service URL is missing from the service description.")
                output.write(f"service_url={service_url}\n")
            print(revision_name)
        else:
            print(json.dumps({"revision": revision_name, "url": release_route["url"]}))
        return 0

    if args.revision:
        if not args.image or not args.expected_revision or not args.resolved_digest:
            raise SystemExit("--image, --resolved-digest and --expected-revision are required for a revision health check.")
        revision = read_json(args.revision)
        revision_name = revision.get("metadata", {}).get("name")
        containers = revision.get("spec", {}).get("containers", [])
        if revision_name != args.expected_revision:
            raise SystemExit("Cloud Run returned an unexpected revision name.")
        if not ready(revision):
            raise SystemExit("The deployed Cloud Run revision is not ready.")
        expected = resolved_image(args.image, args.resolved_digest)
        if not containers or containers[0].get("image") != expected or revision.get("status", {}).get("imageDigest") != expected:
            raise SystemExit("The deployed revision does not reference the expected commit image digest.")
        print(f"Verified ready Cloud Run revision {revision_name}.")
        return 0

    raise SystemExit("Provide either --before and --after, or --revision.")


if __name__ == "__main__":
    raise SystemExit(main())
