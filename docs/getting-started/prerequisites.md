# Prerequisites

## Platform-Level Prerequisites

=== "GitHub"

    ### One-Time Actions Per GitHub User/Organization

    #### GitHub App Installations

    Install each of these apps for the user or organization that will own your repos. Give each
    one access to all repositories, so you only do this once instead of for every new repo.

    1. [Renovate GitHub App](https://github.com/apps/renovate)
        - Opens dependency update PRs.
    1. **[Optional]** [Codecov GitHub App](https://github.com/apps/codecov)
        - Connects the repo to codecov.io for coverage uploads, PR comments, and checks.
        - Skip it if you'll answer no to `code_coverage`.
    1. **[Skip for Private Repos]** [AllContributors GitHub App](https://github.com/apps/allcontributors/installations/new)
        - Credits contributors in the README.

    #### Failed Workflow Alerts

    To get alerts when a workflow run fails, open
    [Notification settings](https://github.com/settings/notifications), choose how to be notified
    under **Actions**, and enable **Only notify for failed workflows**.

    GitHub sends a scheduled workflow's notifications to whoever created the workflow or last
    changed its cron schedule, not to everyone with access to the repo.

=== "Azure DevOps"

    ### One-Time Actions per Azure DevOps Project

    So that squash-merge commit titles stay valid Conventional Commits, turn off **Include PR ID
    in the completion commit message title by default** under **Project settings >
    Repositories > Settings > All Repositories Settings**.

    The release, docs, and Renovate pipelines push branches and open PRs as the pipeline's build
    service identity. Which identity that is depends on **Project settings > Pipelines > Settings >
    Limit job authorization scope to current project for non-release pipelines**: when it's on
    (the default for new projects), pipelines run as `<Project> Build Service (<Org>)`; when it's
    off, as `Project Collection Build Service (<Org>)`.

    Under **Project settings > Repositories > Security**, select that identity and set these to
    **Allow**:

    - `Contribute`
    - `Contribute to pull requests`
    - `Create branch`

## Helm Chart Publishing

A chart project's releases push the chart to its Azure Container Registry. Set this up once per
repo, before its first release.

=== "GitHub"

    The release workflow signs in as a Microsoft Entra app registration, through a federated
    credential for the repo's `main` branch, and needs the AcrPush role on the registry. The
    credential's subject has to match the one in the workflow's token, which can include
    the owner's and repo's numeric IDs; the repo's OIDC settings give its exact start. With
    the Azure CLI signed in to the registry's subscription, and the GitHub CLI in the repo,
    replace `<owner>`, `<repo>`, and `<registry>`, in Bash (Git Bash on Windows):

    ```sh
    client_id=$(az ad app create --display-name <repo> --query appId -o tsv)
    az ad sp create --id "$client_id"
    subject="$(gh api repos/<owner>/<repo>/actions/oidc/customization/sub --jq .sub_claim_prefix):ref:refs/heads/main"
    az ad app federated-credential create --id "$client_id" --parameters "{\"name\": \"github-main\", \"issuer\": \"https://token.actions.githubusercontent.com\", \"subject\": \"$subject\", \"audiences\": [\"api://AzureADTokenExchange\"]}"
    az role assignment create --assignee "$client_id" --role AcrPush --scope "$(az acr show --name <registry> --query id -o tsv)"
    gh variable set AZURE_CLIENT_ID --body "$client_id"
    gh variable set AZURE_TENANT_ID --body "$(az account show --query tenantId -o tsv)"
    gh variable set AZURE_SUBSCRIPTION_ID --body "$(az account show --query id -o tsv)"
    ```

=== "Azure DevOps"

    The publish pipeline signs in through the Azure Resource Manager service connection that
    `acr_service_connection` names. Workload identity federation is the kind that needs no
    stored secret.

    1. Give the service connection's identity the AcrPush role on the registry.
    1. Allow the Helm (Auto) - Publish Release Chart pipeline to use the connection, under the
       connection's **Security** settings, or permit it when its first run asks.

## Workstation Prerequisites

### Universal

1. Install [mise](https://mise.jdx.dev/getting-started.html), which installs every other tool
   this project uses.
1. [Activate mise in your shell](https://mise.jdx.dev/getting-started.html#activate-mise).

### Platform-Specific

=== "GitHub"

    1. Install the [GitHub CLI](https://cli.github.com). Setup uses it to create the repo and
       apply its settings from `.github/settings.yml`, through
       [gh-settings](https://github.com/noirbizarre/gh-settings), which mise installs. Your
       login needs admin access to the repo.
    1. Log in to the GitHub CLI:
        ```
        gh auth login
        ```
    1. Set up Git authentication over HTTPS, with either:
        - `gh auth setup-git`, so Git reuses the GitHub CLI's credentials.
        - [Git Credential Manager](https://github.com/git-ecosystem/git-credential-manager), which
          also handles Azure DevOps. Git for Windows includes it; on macOS and Linux, install it
          separately.

=== "Azure DevOps"

    1. Install the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli).
    1. Install its Azure DevOps extension and log in:
        ```
        az extension add --name azure-devops
        az login --allow-no-subscriptions --use-device-code
        ```
    1. Install [Git Credential Manager](https://github.com/git-ecosystem/git-credential-manager) to
       authenticate Git over HTTPS. Git for Windows includes it; on macOS and Linux, install it
       separately.
    1. Sign Git in to your organization, replacing both `<org>`s, and enter the code it prints at
       the page it names. Git Credential Manager keeps its own sign-in, separate from `az login`:
        ```
        echo url=https://<org>@dev.azure.com/<org>/ | git -c credential.msauthFlow=devicecode credential fill | git credential approve
        ```
