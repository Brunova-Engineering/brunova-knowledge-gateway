# GitHub Actions deployment to Cloud Run

`.github/workflows/production-cloud-run.yml` runs the Python test suite on pushes
to `main`, then deploys only the exact triggering commit when it is still the
current `main` head. A manual run is accepted only from `main`. Images use the
full GitHub commit SHA as the Artifact Registry tag.

The deployment creates a uniquely tagged Cloud Run revision with `--no-traffic`
so production traffic stays unchanged during initial checks. It checks that the
runtime service account, container settings, environment, and revision settings
did not change, then verifies the tagged revision is Ready and answers
`/health`. It calls MCP `initialize` and `tools/list` on that revision using a
dedicated read-only Gateway developer token. Once these checks pass, the workflow
promotes that exact revision to 100% production traffic, then repeats the health
and authenticated MCP catalog checks against the service URL. If either
post-promotion check fails, it restores the captured prior revision traffic
assignments and verifies the rollback before removing only the temporary tag.

## GitHub repository configuration

Configure these repository secrets:

| Secret | Value |
| --- | --- |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Full resource name of the GitHub OIDC WIF provider |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | Email of the narrowly scoped deploy service account |
| `GATEWAY_MCP_VERIFY_TOKEN` | Token for a dedicated active developer principal with read-only MCP access |

Configure these repository variables:

| Variable | Existing production value |
| --- | --- |
| `GCP_PROJECT_ID` | `brunova-ai-platform` |
| `GCP_REGION` | `us-central1` |
| `CLOUD_RUN_SERVICE` | `brunova-knowledge-gateway` |
| `ARTIFACT_REGISTRY_REPOSITORY` | `cloud-run-source-deploy` |

Read-only inspection on 2026-10-22 found the service at revision
`brunova-knowledge-gateway-00099-m5m`, with 100% of production traffic assigned
to that revision and several existing zero-percent tagged revision routes. The
service uses the `brunova-knowledge-agent` runtime identity and listens on port
8080. The workflow reads live configuration before deployment and preserves
the actual traffic assignments it finds at run time.

The MCP token must belong to a dedicated developer principal with at least one
visible read-only tool so the workflow can validate the catalog. Do not use the
management token or a Google service-account key. The workflow separately
mints a short-lived Cloud Run identity token for the deploy service account and
sends it in `X-Serverless-Authorization`; the Gateway token remains in the
application's `Authorization` header. Neither token is printed or embedded in
the image.

## Google Cloud identity setup

An administrator provisions a Workload Identity Federation provider for
`https://token.actions.githubusercontent.com`, maps the GitHub repository
identity claims, and restricts the provider and service-account binding to this
repository and `refs/heads/main`. Prefer the immutable `repository_id` and
`repository_owner_id` claims for identity matching. The provider condition must
allow only this repository and `assertion.ref == 'refs/heads/main'`; permit only
the `push` and `workflow_dispatch` events used by this workflow. Grant the deploy
identity only the permissions needed for the existing resources:

- `roles/run.developer` on the `brunova-knowledge-gateway` Cloud Run service;
- `roles/run.invoker` on the `brunova-knowledge-gateway` service so the workflow
  can call its private health and MCP endpoints;
- `roles/iam.serviceAccountUser` on the existing Cloud Run runtime identity,
  `brunova-knowledge-agent@brunova-ai-platform.iam.gserviceaccount.com`;
- `roles/artifactregistry.writer` on the `cloud-run-source-deploy` repository.

Cloud Run must also be able to import the image. Confirm the existing Cloud Run
service agent has `roles/artifactregistry.reader` on this repository; this is a
runtime/platform permission, not a reason to grant the GitHub deploy identity
broader access.

The workflow uses the `google-github-actions/auth` action with GitHub OIDC and
short-lived credentials and an audience-bound invocation token. The service
currently does not grant `run.invoker` to `allUsers`; keep the service private.
It does not use a service-account key. The Cloud Run
deployment needs Artifact Registry Reader on the image repository for Cloud Run
to import the image; confirm the existing Cloud Run service agent already has
that access before the first workflow run.

## Deployment checks and recovery

The workflow stops before deployment if a required GitHub value is missing, the
triggering SHA is no longer the head of `main`, the image cannot be pushed, or
the current service configuration cannot be read. After creating a tagged
revision, it validates that only the image changed, existing traffic assignments
are preserved, the new revision is ready, the revision health endpoint responds,
and the authenticated MCP catalog is well formed with unique tool names.

The temporary release tag combines `sha-`, the first 12 characters of the
commit SHA, the GitHub run ID, and the run attempt, so reruns use a fresh tag.
The workflow refuses to create a tag that already exists and removes only its
own tag after verification, including when a health or MCP check fails. A failed
post-promotion health or MCP check triggers rollback to the exact
prior active revision traffic percentages captured before deployment. The
workflow does not delete old revisions or existing tags.

The MCP application token is a separate Gateway authorization boundary from
Google WIF. If the verifier token needs rotation, rotate its developer principal
through the governed Gateway process and update the GitHub secret separately.

References: [GitHub Actions WIF setup](https://github.com/google-github-actions/auth),
[Cloud Run deployment permissions](https://docs.cloud.google.com/run/docs/reference/iam/roles),
and [Artifact Registry push permissions](https://docs.cloud.google.com/artifact-registry/docs/docker/pushing-and-pulling).
