"""Create a throwaway Standard repo from this template and verify its published chart.

Run it with `mise run integration-test-helm-gh` or
`mise run integration-test-helm-azdo`. The other integration tests' repos are Template
projects, which don't render the chart or its release workflow, so this makes its own.
It always runs against HEAD, to verify a release candidate on main before a release.

In the Azure subscription and resource group you name, it creates a Basic container
registry and a Microsoft Entra app registration with AcrPush on it. The test repo signs
in as that app: on GitHub through a federated credential for the repo's main branch, on
Azure DevOps through a workload identity service connection. It then runs the release
flow and checks that the release pushed the chart to the registry. The repo and the
Azure resources stay for inspection; delete them with
`mise run integration-test-helm-cleanup-gh` or
`mise run integration-test-helm-cleanup-azdo`.

Without the rights to create those in Azure, pass --registry to use an existing
registry, and an identity that already has AcrPush on it. On GitHub, that's
--client-id, an app registration you own; each run adds a federated credential for its
repo, which cleanup removes. On Azure DevOps, it's --service-connection, an existing
one any pipeline may use. Each run publishes under its own path in the registry, which
cleanup deletes.
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import integration_test_azdo as azdo
import integration_test_github as github
from console import RepoResult, check, heading, run, say, start_log, step, summary
from repo_edits import local_clone_dir, local_clones, prepare_repo_b, remove_clones

CHART = "integration-test-chart"
# Under the registry's helm/ path; each run publishes beneath it, in a folder
# named after its repo, so a shared registry keeps runs apart.
CHART_ROOT = "integration-test"
RELEASE_WORKFLOW = "helm-auto-publishrelease.yml"
HELM_PIPELINE = "Helm (Auto) - Publish Release Chart"
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
# The tag that marks a registry as this test's, naming the test repo it serves.
RESOURCE_TAG = "integration-test"


def name_prefix(repo_prefix: str, platform: str) -> str:
    """Return what every name main() gives its repo and Azure resources starts with.

    Returns:
        `<repo_prefix>-integration-test-helm-<platform>-`, before the run's timestamp.

    """
    return f"{repo_prefix}-integration-test-helm-{platform}-"


def test_repo_pattern(repo_prefix: str, platform: str) -> re.Pattern[str]:
    """Match the names main() gives its test repos on `platform`, and nothing else.

    Returns:
        A pattern for `<repo_prefix>-integration-test-helm-<platform>-<timestamp>`.

    """
    return re.compile(rf"{re.escape(name_prefix(repo_prefix, platform))}\d+")


def az_value(*args: str) -> str:
    """Run `az <args>` and return its trimmed stdout.

    Returns:
        The command's stdout, stripped of surrounding whitespace.

    """
    return run("az", *args, capture=True).stdout.strip()


def az_json(*args: str) -> object:
    """Run `az <args> -o json` and parse its output.

    Returns:
        The parsed JSON.

    """
    return json.loads(az_value(*args, "-o", "json") or "null")


def json_file(directory: str, name: str, content: object) -> str:
    """Write `content` to a JSON file, for an az argument that takes `@<file>`.

    On Windows, az is a batch wrapper, and cmd.exe mangles the quotes in a JSON
    argument; a file sidesteps that.

    Returns:
        The file's path.

    """
    path = Path(directory, name)
    path.write_text(json.dumps(content), encoding="utf-8")
    return str(path)


def check_azure(subscription: str, resource_group: str | None) -> str:
    """Verify az is signed in, and can see the resource group if there is one.

    Returns:
        The subscription's tenant ID.

    Raises:
        SystemExit: if az isn't signed in or can't see the resource group.

    """
    account = run(
        "az", "account", "show", "--subscription", subscription,
        "--query", "tenantId", "-o", "tsv",
        check=False, capture=True,
    )  # fmt: skip
    if account.returncode:
        raise SystemExit(
            f"az can't see subscription {subscription!r}. Sign in with `az login`, "
            "then rerun."
        )
    if not resource_group:
        return account.stdout.strip()
    group = run(
        "az", "group", "show", "--name", resource_group, "--subscription", subscription,
        check=False, capture=True,
    )  # fmt: skip
    if group.returncode:
        raise SystemExit(
            f"az can't see resource group {resource_group!r} in {subscription!r}."
        )
    return account.stdout.strip()


def check_registry(acr: str, subscription: str) -> None:
    """Verify az can read an existing registry's repositories, before creating anything.

    The run reads the published chart's tags at the end, which needs pull access.

    Raises:
        SystemExit: if az can't list the registry's repositories.

    """
    result = run(
        "az", "acr", "repository", "list", "--name", acr,
        "--subscription", subscription, "--top", "1",
        check=False, capture=True,
    )  # fmt: skip
    if result.returncode:
        raise SystemExit(
            f"az can't read registry {acr!r}'s repositories; the run needs pull access "
            f"to check what it published.\n{result.stderr.strip()}"
        )


def chart_prefix(repo_name: str) -> str:
    """Return the run's acr_chart_prefix answer, which names its own registry path.

    Returns:
        `integration-test/<repo_name>`.

    """
    return f"{CHART_ROOT}/{repo_name}"


def registry_name(repo_name: str) -> str:
    """Derive a registry name from the test repo's name.

    Registry names are 5 to 50 letters and digits, and unique across Azure; the repo
    name's timestamp makes this one unique.

    Returns:
        The repo name's letters and digits, trimmed from the left to 50 characters.

    """
    return re.sub(r"[^a-z0-9]", "", repo_name.lower())[-50:]


def create_azure_resources(
    repo_name: str, subscription: str, resource_group: str
) -> tuple[str, str, str]:
    """Create the registry and the app registration that pushes to it.

    Returns:
        The registry's name, the app's client ID, and its service principal's object ID.

    Raises:
        SystemExit: if AcrPush can't be assigned within five minutes.

    """
    acr = registry_name(repo_name)
    step(f"Creating registry {acr}")
    acr_id = az_value(
        "acr", "create", "--name", acr, "--resource-group", resource_group,
        "--subscription", subscription, "--sku", "Basic",
        "--tags", f"{RESOURCE_TAG}={repo_name}",
        "--query", "id", "-o", "tsv",
    )  # fmt: skip
    step(f"Creating app registration {repo_name}")
    client_id = az_value(
        "ad", "app", "create", "--display-name", repo_name,
        "--query", "appId", "-o", "tsv",
    )  # fmt: skip
    principal_id = az_value(
        "ad", "sp", "create", "--id", client_id, "--query", "id", "-o", "tsv"
    )
    step("Giving the app AcrPush on the registry")
    # A new service principal takes a while to reach every Entra replica, and a role
    # assignment made before then fails with PrincipalNotFound.
    deadline = time.time() + 300
    while True:
        assignment = run(
            "az", "role", "assignment", "create",
            "--assignee-object-id", principal_id,
            "--assignee-principal-type", "ServicePrincipal",
            "--role", "AcrPush", "--scope", acr_id,
            check=False, capture=True,
        )  # fmt: skip
        if assignment.returncode == 0:
            break
        if time.time() > deadline:
            raise SystemExit(f"Couldn't assign AcrPush:\n{assignment.stderr}")
        time.sleep(15)
    return acr, client_id, principal_id


def add_federated_credential(
    client_id: str, name: str, issuer: str, subject: str, scratch: str
) -> None:
    """Let tokens from `issuer` for `subject` sign in as the app."""
    parameters = json_file(
        scratch,
        f"{name}.json",
        {
            "name": name,
            "issuer": issuer,
            "subject": subject,
            "audiences": ["api://AzureADTokenExchange"],
        },
    )
    run(
        "az", "ad", "app", "federated-credential", "create",
        "--id", client_id, "--parameters", f"@{parameters}",
        capture=True,
    )  # fmt: skip


def check_chart_published(
    acr: str, subscription: str, repo_name: str, version: str, failures: list[str]
) -> None:
    """Check that the registry has the run's chart at `version`."""
    repository = f"helm/{chart_prefix(repo_name)}/{CHART}"
    tags = run(
        "az", "acr", "repository", "show-tags", "--name", acr,
        "--subscription", subscription, "--repository", repository, "-o", "tsv",
        check=False, capture=True,
    )  # fmt: skip
    check(
        failures,
        f"the registry has {repository}:{version}",
        version in tags.stdout.split(),
        (tags.stdout + tags.stderr).strip(),
    )


def helm_answers(
    acr: str, repo_name: str, service_connection: str = ""
) -> tuple[str, ...]:
    """Return the -d arguments for this template's Helm questions.

    Returns:
        Copier -d arguments.

    """
    answers = [
        "-d", "container_registry_type=Azure Container Registry",
        "-d", f"acr_name={acr}",
        "-d", f"acr_chart_prefix={chart_prefix(repo_name)}",
        "-d", f"helm_chart_name={CHART}",
    ]  # fmt: skip
    if service_connection:
        answers += ["-d", f"acr_service_connection={service_connection}"]
    return tuple(answers)


def run_github(args: argparse.Namespace, scratch: str) -> RepoResult:
    """Create and verify a GitHub test repo.

    Returns:
        The repo's result, for the summary.

    """
    github.check_gh_installed()
    github.check_scopes()
    tenant = check_azure(args.subscription, args.resource_group)
    if args.registry:
        check_registry(args.registry, args.subscription)
    login = github.gh_value("api", "user", "--jq", ".login")
    owner = args.owner or login
    repo_name = f"{name_prefix(args.repo_prefix, 'gh')}{int(time.time())}"
    repo = f"{owner}/{repo_name}"
    dest = local_clone_dir(repo_name)
    failures: list[str] = []

    if args.registry:
        heading(f"A Standard GitHub copy at HEAD, publishing to {args.registry}")
        acr, client_id = args.registry, args.client_id
    else:
        heading("A Standard GitHub copy at HEAD, publishing to a new registry")
        acr, client_id, _ = create_azure_resources(
            repo_name, args.subscription, args.resource_group
        )
    step("Adding a federated credential for the repo's main branch")
    # The release workflow dispatches the publish workflow on main, so its token's
    # subject names that branch. Named after the repo, so cleanup finds it on a
    # shared app.
    add_federated_credential(
        client_id, repo_name, GITHUB_OIDC_ISSUER,
        f"repo:{repo}:ref:refs/heads/main", scratch,
    )  # fmt: skip
    step(f"Creating {repo} at HEAD")
    # leaf=True leaves out the Template answers, so this copy is Standard by default.
    github.create(
        args.source,
        "HEAD",
        repo_name,
        dest,
        owner,
        login,
        "GitHub Pages",
        code_coverage=False,
        leaf=True,
        extra_answers=helm_answers(acr, repo_name),
    )
    step("Setting test secrets and the Azure sign-in variables")
    github.provision_test_secrets(repo)
    for name, value in (
        ("AZURE_CLIENT_ID", client_id),
        ("AZURE_TENANT_ID", tenant),
        ("AZURE_SUBSCRIPTION_ID", args.subscription),
    ):
        run("gh", "variable", "set", name, "--body", value, "--repo", repo)
    if setup := prepare_repo_b(args.source):
        step("Merging the setup PR")
        github.open_pr_wait_and_merge(
            repo,
            dest,
            "integration-test/setup",
            "chore: set up the repo for the integration test",
            edit=setup,
        )

    github.check_release_flow(repo, dest, failures)
    step("Checking Helm (Auto): Publish Release Chart")
    release = run(
        "gh", "release", "view", "--repo", repo,
        "--json", "tagName", "-q", ".tagName",
        check=False, capture=True,
    )  # fmt: skip
    check(failures, "the repo has a release", release.returncode == 0)
    if release.returncode == 0:
        # Raises SystemExit, naming the workflow, if Publish Release didn't dispatch it.
        run_id = github.wait_for_new_run(
            repo, RELEASE_WORKFLOW, "", event="workflow_dispatch"
        )
        conclusion = github.wait_for_run(repo, run_id)
        check(failures, "the publish chart run succeeds", conclusion == "success")
        version = release.stdout.strip().removeprefix("v")
        check_chart_published(acr, args.subscription, repo_name, version, failures)
    return RepoResult(
        "Helm repo", repo, dest, failures, url=f"https://github.com/{repo}"
    )


def create_service_connection(
    org_url: str,
    project: str,
    name: str,
    client_id: str,
    tenant: str,
    subscription: str,
    scratch: str,
) -> None:
    """Create a workload identity service connection that signs in as the app.

    Azure DevOps issues the connection's token, so the app gets a federated credential
    for the issuer and subject Azure DevOps assigns it. Every pipeline in the project
    may use it, so the Helm pipeline's first run doesn't wait for someone to permit it.
    """
    scope = ("--org", org_url, "--project", project)
    project_id = az_value(
        "devops", "project", "show", *scope, "--query", "id", "-o", "tsv"
    )
    subscription_name = az_value(
        "account",
        "show",
        "--subscription",
        subscription,
        "--query",
        "name",
        "-o",
        "tsv",
    )
    config = json_file(
        scratch,
        "service-connection.json",
        {
            "name": name,
            "type": "AzureRM",
            "url": "https://management.azure.com/",
            "authorization": {
                "scheme": "WorkloadIdentityFederation",
                "parameters": {"tenantid": tenant, "serviceprincipalid": client_id},
            },
            "data": {
                "subscriptionId": subscription,
                "subscriptionName": subscription_name,
                "environment": "AzureCloud",
                "scopeLevel": "Subscription",
                "creationMode": "Manual",
            },
            "isShared": False,
            "isReady": True,
            "serviceEndpointProjectReferences": [
                {"projectReference": {"id": project_id, "name": project}, "name": name}
            ],
        },
    )
    endpoint_id = az_value(
        "devops", "service-endpoint", "create",
        "--service-endpoint-configuration", config, *scope,
        "--query", "id", "-o", "tsv",
    )  # fmt: skip
    parameters = az_json(
        "devops", "service-endpoint", "show", "--id", endpoint_id, *scope,
        "--query", "authorization.parameters",
    )  # fmt: skip
    add_federated_credential(
        client_id,
        "azure-devops",
        parameters["workloadIdentityFederationIssuer"],
        parameters["workloadIdentityFederationSubject"],
        scratch,
    )
    run(
        "az", "devops", "service-endpoint", "update", "--id", endpoint_id,
        "--enable-for-all", "true", *scope,
        capture=True,
    )  # fmt: skip


def wait_for_helm_run(
    org_url: str, project: str, repo_name: str, commit: str, *, timeout: int = 1800
) -> str:
    """Wait for the Helm pipeline's run on `commit` to finish.

    The release pipeline's completion triggers it, at the commit that run built.

    Returns:
        The run's result, or "" if no run on `commit` finished within `timeout`.

    """
    pid = azdo.pipeline_numeric_id(org_url, project, f"[{repo_name}] {HELM_PIPELINE}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = az_json(
            "pipelines", "runs", "list", "--pipeline-ids", pid,
            "--org", org_url, "--project", project,
            "--query", "[].{status:status,result:result,sourceVersion:sourceVersion}",
        )  # fmt: skip
        for helm_run in runs or []:
            if (
                helm_run["sourceVersion"] == commit
                and helm_run["status"] == "completed"
            ):
                return helm_run["result"]
        time.sleep(15)
    return ""


def run_azdo(args: argparse.Namespace, scratch: str) -> RepoResult:
    """Create and verify an Azure DevOps test repo.

    Returns:
        The repo's result, for the summary.

    Raises:
        SystemExit: if --org or --project is missing.

    """
    azdo.check_az_installed()
    azdo.check_azure_devops_extension()
    if not args.org or not args.project:
        raise SystemExit(
            "--org/--project are required. Pass them, or set "
            "INTEGRATION_TEST_AZDO_ORG and INTEGRATION_TEST_AZDO_PROJECT, such as in "
            "mise.local.toml's [env]."
        )
    org_url = f"https://dev.azure.com/{args.org}/"
    azdo.check_access(org_url, args.project)
    tenant = check_azure(args.subscription, args.resource_group)
    if args.registry:
        check_registry(args.registry, args.subscription)
    token = azdo.github_token()
    repo_name = f"{name_prefix(args.repo_prefix, 'azdo')}{int(time.time())}"
    dest = local_clone_dir(repo_name)
    failures: list[str] = []

    if args.registry:
        heading(f"A Standard Azure DevOps copy at HEAD, publishing to {args.registry}")
        acr, connection = args.registry, args.service_connection
    else:
        heading("A Standard Azure DevOps copy at HEAD, publishing to a new registry")
        acr, client_id, _ = create_azure_resources(
            repo_name, args.subscription, args.resource_group
        )
        connection = repo_name
        step(f"Creating service connection {connection}")
        create_service_connection(
            org_url, args.project, connection, client_id, tenant,
            args.subscription, scratch,
        )  # fmt: skip
    step(f"Creating {repo_name} at HEAD")
    azdo.create(
        args.source,
        "HEAD",
        repo_name,
        dest,
        args.org,
        args.project,
        code_coverage=False,
        leaf=True,
        extra_answers=helm_answers(acr, repo_name, service_connection=connection),
    )
    step("Setting test secrets")
    azdo.provision_test_secrets(
        org_url,
        args.project,
        repo_name,
        token,
        names=(*azdo.PIPELINE_DISPLAY_NAMES, HELM_PIPELINE),
    )
    if setup := prepare_repo_b(args.source):
        step("Merging the setup PR")
        azdo.open_pr_wait_and_merge(
            org_url,
            args.project,
            repo_name,
            dest,
            "integration-test/setup",
            "chore: set up the repo for the integration test",
            edit=setup,
        )

    azdo.check_release_flow(org_url, args.project, repo_name, dest, failures)
    step(f"Checking {HELM_PIPELINE}")
    tags = sorted(
        (
            tag
            for tag in azdo.list_tags(org_url, args.project, repo_name)
            if re.fullmatch(r"v\d+\.\d+\.\d+", tag)
        ),
        key=lambda tag: tuple(int(part) for part in tag[1:].split(".")),
    )
    check(failures, "the repo has a release tag", bool(tags))
    if tags:
        run("git", "-C", dest, "fetch", "--tags", "--quiet", "origin", capture=True)
        commit = run(
            "git", "-C", dest, "rev-list", "-n", "1", tags[-1], capture=True
        ).stdout.strip()
        result = wait_for_helm_run(org_url, args.project, repo_name, commit)
        check(
            failures,
            "the release commit's chart run succeeds",
            result == "succeeded",
            # A run that never finished is likely waiting for permission to use the
            # service connection.
            result or "no finished run; check that the pipeline may use the connection",
        )
        check_chart_published(acr, args.subscription, repo_name, tags[-1][1:], failures)
    return RepoResult(
        "Helm repo",
        repo_name,
        dest,
        failures,
        url=azdo.repo_url(org_url, args.project, repo_name),
    )


# A thing cleanup() deletes: what it lists, and the command that deletes it.
Deletion = tuple[str, tuple[str, ...]]


def find_azure_resources(
    prefix: str, subscription: str, resource_group: str
) -> list[Deletion]:
    """Find this test's registries and app registrations in Azure, by `prefix`.

    Deleting an app registration deletes its service principal too, and a role
    assignment goes with the registry it's on.

    Returns:
        The registries, then the apps.

    """
    pattern = re.compile(rf"{re.escape(prefix)}\d+")
    registries = az_json(
        "acr", "list", "--resource-group", resource_group,
        "--subscription", subscription,
        "--query", f'[].{{name:name,id:id,repo:tags."{RESOURCE_TAG}"}}',
    )  # fmt: skip
    apps = az_json(
        "ad", "app", "list", "--filter", f"startswith(displayName,'{prefix}')",
        "--query", "[].{name:displayName,appId:appId}",
    )  # fmt: skip
    return [
        *(
            (
                f"registry {r['name']}",
                ("az", "acr", "delete", "--ids", r["id"], "--yes"),
            )
            for r in registries or []
            if r["repo"] and pattern.fullmatch(r["repo"])
        ),
        *(
            (
                f"app registration {a['name']}",
                ("az", "ad", "app", "delete", "--id", a["appId"]),
            )
            for a in apps or []
            if pattern.fullmatch(a["name"])
        ),
    ]


def find_shared_resources(
    args: argparse.Namespace, pattern: re.Pattern[str]
) -> list[Deletion]:
    """Find what runs left in an existing registry and app registration.

    Returns:
        The charts each run published to --registry, then the federated credentials
        each run added to --client-id.

    """
    deletions: list[Deletion] = []
    if args.registry:
        repositories = az_json(
            "acr", "repository", "list", "--name", args.registry,
            "--subscription", args.subscription,
        )  # fmt: skip
        run_path = re.compile(rf"helm/{re.escape(CHART_ROOT)}/({pattern.pattern})/.+")
        deletions += [
            (
                f"chart {args.registry}/{repository}",
                (
                    "az",
                    "acr",
                    "repository",
                    "delete",
                    "--name",
                    args.registry,
                    "--subscription",
                    args.subscription,
                    "--repository",
                    repository,
                    "--yes",
                ),
            )  # fmt: skip
            for repository in repositories or []
            if run_path.fullmatch(repository)
        ]
    if args.client_id:
        credentials = az_json(
            "ad", "app", "federated-credential", "list", "--id", args.client_id,
            "--query", "[].{id:id,name:name}",
        )  # fmt: skip
        deletions += [
            (
                f"federated credential {c['name']}",
                (
                    "az",
                    "ad",
                    "app",
                    "federated-credential",
                    "delete",
                    "--id",
                    args.client_id,
                    "--federated-credential-id",
                    c["id"],
                ),
            )  # fmt: skip
            for c in credentials or []
            if pattern.fullmatch(c["name"])
        ]
    return deletions


def find_github_repos(
    args: argparse.Namespace, pattern: re.Pattern[str]
) -> list[Deletion]:
    """Find this test's GitHub repos under --owner.

    Returns:
        The repos.

    """
    github.check_gh_installed()
    owner = args.owner or github.gh_value("api", "user", "--jq", ".login")
    repos = json.loads(
        github.gh_value("repo", "list", owner, "--limit", "1000", "--json", "name")
    )
    return [
        (
            f"repo {owner}/{r['name']}",
            ("gh", "repo", "delete", f"{owner}/{r['name']}", "--yes"),
        )
        for r in repos
        if pattern.fullmatch(r["name"])
    ]


def find_azdo_resources(
    args: argparse.Namespace, pattern: re.Pattern[str]
) -> list[Deletion]:
    """Find this test's pipelines, service connections, and repos in the project.

    Deleting a repo leaves its pipelines behind, so they're matched by their
    `[<repo>] ` name prefix, and deleted first.

    Returns:
        The pipelines, then the service connections, then the repos.

    Raises:
        SystemExit: if --org or --project is missing.

    """
    if not args.org or not args.project:
        raise SystemExit("--org/--project are required to clean up Azure DevOps.")
    scope = ("--org", f"https://dev.azure.com/{args.org}/", "--project", args.project)
    query = ("--query", "[].{id:id,name:name}")
    pipeline_name = re.compile(rf"\[({pattern.pattern})\] ")
    pipelines = az_json("pipelines", "list", *scope, *query)
    connections = az_json("devops", "service-endpoint", "list", *scope, *query)
    repos = az_json("repos", "list", *scope, *query)
    return [
        *(
            (
                f"pipeline {x['name']}",
                ("az", "pipelines", "delete", "--id", str(x["id"]), *scope, "--yes"),
            )
            for x in pipelines
            if pipeline_name.match(x["name"])
        ),
        *(
            (
                f"service connection {x['name']}",
                (
                    "az",
                    "devops",
                    "service-endpoint",
                    "delete",
                    "--id",
                    x["id"],
                    *scope,
                    "--yes",
                ),
            )
            for x in connections
            if pattern.fullmatch(x["name"])
        ),
        *(
            (
                f"repo {x['name']}",
                ("az", "repos", "delete", "--id", x["id"], *scope, "--yes"),
            )
            for x in repos
            if pattern.fullmatch(x["name"])
        ),
    ]


def cleanup(args: argparse.Namespace) -> None:
    """Delete every repo and Azure resource main() created on `args.platform`.

    It finds them by name, so it covers runs that stopped partway. It lists everything,
    then asks.

    Raises:
        SystemExit: if a local clone can't be deleted.

    """
    pattern = test_repo_pattern(args.repo_prefix, args.platform)
    check_azure(args.subscription, args.resource_group)
    if args.platform == "gh":
        deletions = find_github_repos(args, pattern)
        clones = local_clones(pattern, "github.com")
    else:
        deletions = find_azdo_resources(args, pattern)
        clones = local_clones(pattern, "dev.azure.com")
    deletions += find_shared_resources(args, pattern)
    if args.resource_group:
        deletions += find_azure_resources(
            name_prefix(args.repo_prefix, args.platform),
            args.subscription,
            args.resource_group,
        )
    if not deletions and not clones:
        say("No Helm integration-test repos, Azure resources, or clones to delete.")
        return
    say("Helm integration-test resources and local clones to delete:")
    for label, _ in deletions:
        say(f"  {label}")
    for clone in clones:
        say(f"  clone {clone}")
    if not github.confirm("Delete all of these?", yes=args.yes):
        say("Nothing deleted.")
        return
    for _, command in deletions:
        run(*command)
    deleted = remove_clones(clones)
    say(f"Deleted {len(deletions)} resource(s) and {deleted} local clone(s).")
    if deleted < len(clones):
        raise SystemExit(1)


def check_args(args: argparse.Namespace) -> None:
    """Check that the Azure arguments name either new resources or existing ones.

    Raises:
        SystemExit: if a required Azure argument is missing.

    """
    hint = "Pass it, or set {} in mise.local.toml's [env]."
    if not args.subscription:
        raise SystemExit(
            "--subscription is required. "
            + hint.format("INTEGRATION_TEST_AZURE_SUBSCRIPTION")
        )
    if not args.registry and not args.resource_group:
        raise SystemExit(
            "--resource-group is required, for the run to create a registry in; or "
            "pass --registry to use an existing one. "
            + hint.format("INTEGRATION_TEST_AZURE_RESOURCE_GROUP")
        )
    if args.registry and not args.cleanup:
        if args.platform == "gh" and not args.client_id:
            raise SystemExit(
                "--registry needs --client-id on GitHub: an app registration you own "
                "with AcrPush on the registry. "
                + hint.format("INTEGRATION_TEST_AZURE_CLIENT_ID")
            )
        if args.platform == "azdo" and not args.service_connection:
            raise SystemExit(
                "--registry needs --service-connection on Azure DevOps: one with "
                "AcrPush on the registry that any pipeline may use. "
                + hint.format("INTEGRATION_TEST_AZDO_SERVICE_CONNECTION")
            )


def main() -> None:
    """Create and verify one throwaway Standard repo, or clean up past runs.

    Raises:
        SystemExit: if required arguments are missing, or if any check failed (after
            printing every check's result; see check()).

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("gh", "azdo"), required=True)
    parser.add_argument("--source", help="Copier template source URL")
    parser.add_argument("--repo-prefix", required=True, help="This repo's own name")
    # mise passes the tasks' `usage` flags as usage_* environment variables.
    parser.add_argument(
        "--subscription",
        default=os.environ.get("usage_subscription")
        or os.environ.get("INTEGRATION_TEST_AZURE_SUBSCRIPTION"),
        help="Azure subscription for the test registry",
    )
    parser.add_argument(
        "--resource-group",
        default=os.environ.get("usage_resource_group")
        or os.environ.get("INTEGRATION_TEST_AZURE_RESOURCE_GROUP"),
        help="Resource group for the test registry",
    )
    parser.add_argument(
        "--registry",
        default=os.environ.get("usage_registry")
        or os.environ.get("INTEGRATION_TEST_AZURE_REGISTRY"),
        help="Existing registry to publish to, instead of creating one",
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("usage_client_id")
        or os.environ.get("INTEGRATION_TEST_AZURE_CLIENT_ID"),
        help="GitHub: existing app registration with AcrPush on --registry",
    )
    parser.add_argument(
        "--service-connection",
        default=os.environ.get("usage_service_connection")
        or os.environ.get("INTEGRATION_TEST_AZDO_SERVICE_CONNECTION"),
        help="Azure DevOps: existing service connection with AcrPush on --registry",
    )
    parser.add_argument(
        "--owner",
        default=os.environ.get("usage_owner"),
        help="GitHub user or organization for the test repo",
    )
    parser.add_argument(
        "--org",
        default=os.environ.get("usage_org")
        or os.environ.get("INTEGRATION_TEST_AZDO_ORG"),
        help="Azure DevOps organization",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("usage_project")
        or os.environ.get("INTEGRATION_TEST_AZDO_PROJECT"),
        help="Azure DevOps project",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Skip creating a repo; delete every repo and Azure resource a run made.",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Don't ask before --cleanup deletes."
    )
    args = parser.parse_args()
    check_args(args)
    log_dir = start_log(f"helm-{args.platform}")
    say(f"Logging this run to {log_dir}")

    if args.cleanup:
        cleanup(args)
        return
    if not args.source:
        raise SystemExit("--source is required.")
    with tempfile.TemporaryDirectory() as scratch:
        if args.platform == "gh":
            result = run_github(args, scratch)
        else:
            result = run_azdo(args, scratch)
    summary([result])
    if result.failures:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nStopped. Run the cleanup task to delete what this run created.")
