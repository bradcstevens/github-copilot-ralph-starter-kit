---
name: release
description: Release an azd project on GitHub Actions end-to-end — configure CI/CD if needed, push local changes, then trigger the workflow, block on its completion, validate the live deployment, and run backend tests plus Playwright browser tests against the deployed endpoints. Use when the user wants to ship a change ("release this", "deploy and verify", "ship it"), set up automated deployments, wire up GitHub Actions for an azd template, configure federated credentials for Azure, push an azd project to GitHub with a working deployment workflow, update an existing azure-dev.yml workflow, or confirm a deployment actually works in production. Fails the release if the workflow, deployment health probes, backend tests, or Playwright tests fail. Composes with the microsoft/azure-skills plugin — runs after azure-deploy has successfully deployed once locally (recommended), or after azure-prepare + azure-validate for a CI/CD-first workflow.
---

# release

Operationalize an azd project on GitHub Actions: provision federated identity, create or update the deployment workflow, push local changes, **run the pipeline, verify the deployment is healthy, and prove it works by running backend tests plus Playwright frontend tests against the live endpoints**. The skill does not consider itself complete until all of that has passed.

## Scope

This skill handles the **operationalization _and_ release-verification** steps — taking a working (or validated) azd project, making it deploy automatically from GitHub, and proving the deployment is healthy and passes its tests. It does NOT:

- Generate infrastructure code → use `azure-prepare`
- Validate deployment readiness (static / pre-deploy) → use `azure-validate`
- Run a direct local deployment → use `azure-deploy`
- Configure Azure DevOps Pipelines (this skill is GitHub-only; for ADO use `azd pipeline config --provider azdo`)
- Write the backend or Playwright tests themselves — this skill executes existing tests; if none exist it surfaces that gap and asks the user how to proceed

**Composition with microsoft/azure-skills:**

- `azure-prepare` → `azure-validate` → `azure-deploy` → **`release`** (recommended — prove deployment works locally, then automate it)
- `azure-prepare` → `azure-validate` → **`release`** (CI/CD-first — let the pipeline run the first deploy)

## Global Rules

- ❌ **Destructive git actions require `ask_user`** — force pushes, branch deletions, history rewrites, `git reset --hard`
- ❌ **Never commit secrets or credentials** — credentials live in GitHub Variables (for OIDC) or Secrets (for legacy SP); never in repo files
- ⛔ **Federated identity (OIDC) is the default** — only fall back to client-credentials SP if the user explicitly requests it AND confirms they accept secret rotation
- ⛔ **Never push directly to `main`/`master` without `ask_user` confirmation** showing the exact branch name — prefer a feature branch + `gh pr create`
- ⛔ **Workflow YAML changes are commits** — always show the diff before staging
- ✅ **Use `azd pipeline config` as the primary configuration mechanism** — it handles SP creation, federated cred trust policy, and GitHub var/secret population correctly. Don't reinvent it.
- ⛔ **A release is not "done" until the workflow run completes successfully AND post-deploy validation, backend tests, and Playwright frontend tests all pass.** Surface any failure to the user; never silently mark a failing release as complete.
- ✅ **Run Playwright against the deployed URL, not localhost.** Read the endpoint from `azd env get-values` (or workflow outputs) and pass it via `PLAYWRIGHT_BASE_URL` / `BASE_URL`. Never assume a local dev server.

## Prerequisites

Before doing real work, verify each of these. If any fails, stop and resolve before continuing.

1. **Azure subscription context** — `az account show` returns the expected subscription, OR confirm via `ask_user`
2. **azd project exists** — `azure.yaml` is present at the repo root
3. **Deployment readiness** — `.azure/deployment-plan.md` status is either `Deployed` (post-`azure-deploy` path) or `Ready for Deployment` (CI/CD-first path)
4. **GitHub CLI authenticated** — `gh auth status` returns success
5. **Git remote configured** — `git remote -v` shows an `origin` on github.com, OR `ask_user` for the target repo (and create it in Step 2 Path A)
6. **Test surface discovered** — detect what exists so Step 6 can run it. Capture the results for later steps; missing pieces are not fatal here but must be surfaced before push:
   - **Backend tests** — inspect `azure.yaml` services and each service's manifest:
     - Python: `pytest.ini` / `pyproject.toml [tool.pytest.ini_options]` / `tests/` → `pytest`
     - .NET: `*.Tests.csproj` / `*.sln` with test projects → `dotnet test`
     - Node: `package.json` `scripts.test` (jest/vitest/mocha) → `npm test` / `npm run test:ci`
     - Java: `pom.xml` / `build.gradle` → `mvn test` / `./gradlew test`
     - Go: `*_test.go` → `go test ./...`
   - **Playwright frontend tests** — look for any of: `playwright.config.{ts,js,mjs}`, `e2e/`, `tests-e2e/`, `tests/e2e/`, `@playwright/test` in `package.json`. Note the working directory.
   - If a frontend service is declared in `azure.yaml` but **no Playwright config exists**, `ask_user` whether to (a) scaffold a minimal Playwright smoke test (one `homepage loads` spec), (b) skip frontend tests for this release with explicit acknowledgement, or (c) abort. Default suggestion: (a).
   - If a backend service is declared but **no backend tests are detected**, `ask_user` whether to skip backend tests for this release with explicit acknowledgement, or abort. Default suggestion: ask the user — do not auto-skip.

## Workflow

### Step 1 — Gather context

Run these in parallel and summarize for the user before any decision:

```bash
git status --short
git remote -v
git log --oneline -5
gh auth status
ls -la .github/workflows/ 2>/dev/null
cat azure.yaml 2>/dev/null
head -20 .azure/deployment-plan.md 2>/dev/null
azd env list 2>/dev/null
```

Report concisely:

- Current branch + dirty state
- Existing `origin` and whether it's GitHub
- Whether `.github/workflows/azure-dev.yml` already exists (and its mtime)
- The services declared in `azure.yaml`
- Deployment plan status
- Active azd environment(s)

### Step 2 — Decide path

Pick based on Step 1 results:

| State | Path |
|---|---|
| No GitHub remote | **A** — create repo, then continue to B |
| GitHub remote, no `azure-dev.yml` | **B** — fresh pipeline config |
| `azure-dev.yml` exists | **C** — update / reconcile |

**Path A — Create GitHub repo:**

```bash
gh repo create <name> --source=. --remote=origin --private  # confirm visibility via ask_user
```

`ask_user` for: repo name (default to current directory name), visibility (private/public), org/owner.

**Path B — Fresh config:** proceed to Step 3.

**Path C — Update existing workflow:**

```bash
diff .github/workflows/azure-dev.yml <(curl -s <reference-template-url>)
```

Or render the reference template below and show a structural diff. Propose changes via `ask_user` (path filters, concurrency, environments, manual dispatch). Apply only confirmed changes.

### Step 3 — Run `azd pipeline config`

This is the load-bearing step. It creates the Azure service principal, configures federated credentials for the GitHub repo, and populates GitHub variables:

```bash
azd pipeline config --provider github --auth-type federated
```

Capture stdout. The command will:

- Create or reuse an Azure service principal scoped to the current subscription/resource group
- Configure federated identity trust for `repo:<owner>/<repo>:ref:refs/heads/main` (and additional subjects as needed)
- Populate GitHub repo variables: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_ENV_NAME`, `AZURE_LOCATION`
- Generate a baseline `.github/workflows/azure-dev.yml` if missing

If the user explicitly requests client-credentials (legacy CI scenarios, on-prem agents that can't do OIDC):

```bash
azd pipeline config --provider github --auth-type client-credentials
```

`ask_user` to confirm they accept secret rotation responsibility before using this path.

### Step 4 — Customize the workflow

Even after `azd pipeline config`, the generated workflow is minimal. Diff against the reference template (below) and offer:

- **Path filters** — skip on `*.md`, `docs/**`, `.gitignore` changes
- **Branch strategy** — deploy on `main`, validate-only on PRs
- **Concurrency** — group per ref, `cancel-in-progress: false` (don't cancel deploys mid-flight)
- **`workflow_dispatch`** — manual re-deploy with optional env input
- **Environment promotion** — separate jobs for dev/staging/prod gated by GitHub Environment protection rules
- **What-if on PR** — `azd provision --preview` for review

Show the diff. Apply only confirmed changes.

### Step 5 — Stage, commit, push

Show the user what will be committed before touching git:

```bash
git status
git diff --stat
git diff .github/workflows/
```

With `ask_user` confirmation:

```bash
git add .github/workflows/ azure.yaml infra/ .azure/config.json
# do NOT add .azure/<env>/.env — it contains environment-specific values
git commit -m "ci: configure azd GitHub Actions deployment

- Add/update .github/workflows/azure-dev.yml
- Configure federated identity (OIDC) for Azure deployment
- Wire azd environment '<env-name>' to CI/CD"
```

For the push:

- **Feature branch (preferred):** `git push -u origin <branch>` then `gh pr create --fill --web`
- **Direct to main (requires explicit `ask_user` showing the branch name):** `git push origin main`

⛔ If `git push` is rejected (non-fast-forward), STOP. Show `git fetch && git status` output. `ask_user` before any rebase, merge, or force operation. Never `--force` without explicit user instruction.

### Step 6 — Trigger, watch, and verify the release end-to-end

This step is the gate for "done". Run it in order and treat **any** failure as a failed release.

#### 6.1 — Locate or trigger the workflow run

After the push in Step 5, give GitHub a moment to register it, then find the run for this commit (do **not** rely on `gh run list` ordering alone — match on the head SHA):

```bash
sleep 5
HEAD_SHA="$(git rev-parse HEAD)"
RUN_ID="$(gh run list --workflow=azure-dev.yml --commit "$HEAD_SHA" --limit 1 --json databaseId --jq '.[0].databaseId')"
```

If `$RUN_ID` is empty after up to ~30 seconds of polling (path filters skipped it, or push wasn't to a trigger branch), explicitly dispatch:

```bash
gh workflow run azure-dev.yml --ref "$(git rev-parse --abbrev-ref HEAD)"
sleep 5
RUN_ID="$(gh run list --workflow=azure-dev.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
```

#### 6.2 — Wait for the run to complete

Block on the run and fail loudly on non-zero exit:

```bash
gh run watch "$RUN_ID" --exit-status
```

If `gh run watch` exits non-zero, dump the failed-job logs and stop:

```bash
gh run view "$RUN_ID" --log-failed
```

⛔ Do NOT proceed to validation or tests if the workflow itself failed. Surface the failure to the user with the run URL (`gh run view "$RUN_ID" --web` or the URL from `gh run view`).

#### 6.3 — Validate the deployment is healthy

Even a green workflow doesn't guarantee a working app. Read the live endpoints from azd and hit them:

```bash
azd env get-values > /tmp/azd-env.$$
# Common keys: SERVICE_<NAME>_URI, AZURE_RESOURCE_GROUP, AZURE_LOCATION, plus any outputs the bicep/terraform exports
grep -E '^(SERVICE_.*_URI|.*_ENDPOINT|.*_URL)=' /tmp/azd-env.$$
```

For each discovered endpoint, probe a health path. Try (in order) `/health`, `/healthz`, `/api/health`, `/`. Accept 2xx; treat 3xx as a redirect to follow once; flag 4xx/5xx:

```bash
for url in $ENDPOINTS; do
  for path in /health /healthz /api/health /; do
    code="$(curl -fsS -L -o /dev/null -w '%{http_code}' --max-time 15 "$url$path" || echo 000)"
    [ "$code" = "200" ] && { echo "OK  $url$path"; break; }
  done
done
```

If no probe returns 2xx for a given service, STOP and surface the failure. `ask_user` whether to retry after a brief warm-up wait (cold-start services can take 60-120s) or abort.

#### 6.4 — Run backend tests

Use the framework detected in Prerequisite #6. Run from the service's working directory. Examples:

```bash
# Python (per service dir from azure.yaml)
( cd "$BACKEND_DIR" && python -m pytest -q --maxfail=1 )

# .NET
dotnet test --nologo --verbosity minimal

# Node
( cd "$BACKEND_DIR" && npm ci && npm test --silent )

# Java
( cd "$BACKEND_DIR" && mvn -q -B test )

# Go
( cd "$BACKEND_DIR" && go test ./... )
```

Capture exit code. Non-zero → STOP, show failing test output, do not run frontend tests, and surface to user.

> If backend tests already ran inside the workflow (recommended — see Reference workflow template), you can satisfy this step by confirming the corresponding workflow job succeeded in 6.2 and noting "covered by `test-backend` job in run $RUN_ID". Do not skip silently; record which path was taken.

#### 6.5 — Run Playwright frontend tests against the deployed URL

Frontend tests **must** run against the deployed endpoint, not localhost. Resolve the public URL of the frontend service from `azd env get-values` (typically `SERVICE_WEB_URI`, `SERVICE_FRONTEND_URI`, or a custom output) and pass it through.

Install Playwright if missing (`ask_user` first — this writes to `package.json` / `package-lock.json`):

```bash
# from the frontend project directory
if ! npx --no-install playwright --version >/dev/null 2>&1; then
  npm install --save-dev @playwright/test
  npx playwright install --with-deps   # downloads browsers; --with-deps may need sudo on some CI images
fi
```

Run the suite:

```bash
export PLAYWRIGHT_BASE_URL="$FRONTEND_URL"
export BASE_URL="$FRONTEND_URL"   # for configs that read BASE_URL instead
npx playwright test --reporter=line,html
```

On failure:

- Save the HTML report and trace: `playwright-report/` and `test-results/` (the workflow uploads these as artifacts — see template).
- Print the first failing test's name and `expect()` diff to the user.
- STOP. Do not mark the release complete.

> If Playwright already ran inside the workflow against the deployed URL (recommended), you can satisfy this step by confirming the `test-frontend-playwright` job succeeded in 6.2. Same rule as 6.4 — record which path was taken.

#### 6.6 — Report

Summarize for the user with exact references:

- Workflow run: URL + conclusion
- Each deployed endpoint: URL + health probe result
- Backend tests: framework, pass/fail counts, source (local re-run vs workflow job)
- Playwright tests: pass/fail counts, source, link to HTML report (local path or artifact URL)

Only after every check above is green do you move to Step 7.

### Step 7 — Update the deployment plan

⛔ Before completing, append to `.azure/deployment-plan.md`:

```markdown
## CI/CD Configuration

- Provider: GitHub Actions
- Workflow: .github/workflows/azure-dev.yml
- Auth: Federated identity (OIDC)
- Service principal: <name from azd pipeline config output>
- GitHub repo: <owner>/<repo>
- Triggers: push to main, pull_request, workflow_dispatch
- First run: <gh run URL>
- Status: Configured

## Release Verification (Step 6)

- Run ID: <RUN_ID> — conclusion: success
- Endpoints probed:
  - <service>: <url> → 200 on <path>
- Backend tests: <framework> — <N passed / 0 failed> — source: <workflow job | local re-run>
- Frontend tests (Playwright): <N passed / 0 failed> against <FRONTEND_URL> — source: <workflow job | local re-run>
- HTML report: <path or artifact URL>
- Verified at: <ISO-8601 timestamp>
```

Commit this update with `chore: update deployment plan with CI/CD status` and push.

## Reference workflow template

Use this as the target shape for Step 4 reconciliation. It deploys, then validates the deployment, then runs backend tests and Playwright frontend tests **against the deployed URL** — failing the run if any check fails. Adapt the per-service commands (`pytest`/`dotnet test`/`go test`/etc.) to the stack you detected in Prerequisite #6.

```yaml
name: Deploy azd project

on:
  push:
    branches: [main]
    paths-ignore:
      - '**.md'
      - '.gitignore'
      - 'docs/**'
  pull_request:
    branches: [main]
    paths-ignore:
      - '**.md'
      - 'docs/**'
  workflow_dispatch:

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: false

permissions:
  id-token: write   # required for federated identity
  contents: read

jobs:
  validate:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install azd
        uses: Azure/setup-azd@v2
      - name: Log in with azd (federated)
        run: |
          azd auth login \
            --client-id "$AZURE_CLIENT_ID" \
            --federated-credential-provider "github" \
            --tenant-id "$AZURE_TENANT_ID"
        env:
          AZURE_CLIENT_ID: ${{ vars.AZURE_CLIENT_ID }}
          AZURE_TENANT_ID: ${{ vars.AZURE_TENANT_ID }}
      - name: azd provision (what-if)
        run: azd provision --preview --no-prompt
        env:
          AZURE_ENV_NAME: ${{ vars.AZURE_ENV_NAME }}
          AZURE_LOCATION: ${{ vars.AZURE_LOCATION }}
          AZURE_SUBSCRIPTION_ID: ${{ vars.AZURE_SUBSCRIPTION_ID }}

  deploy:
    if: github.event_name == 'push' || github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-latest
    environment: production   # configure protection rules in repo settings
    outputs:
      frontend_url: ${{ steps.endpoints.outputs.frontend_url }}
      backend_url:  ${{ steps.endpoints.outputs.backend_url }}
    steps:
      - uses: actions/checkout@v4
      - name: Install azd
        uses: Azure/setup-azd@v2
      - name: Log in with azd (federated)
        run: |
          azd auth login \
            --client-id "$AZURE_CLIENT_ID" \
            --federated-credential-provider "github" \
            --tenant-id "$AZURE_TENANT_ID"
        env:
          AZURE_CLIENT_ID: ${{ vars.AZURE_CLIENT_ID }}
          AZURE_TENANT_ID: ${{ vars.AZURE_TENANT_ID }}
      - name: azd provision
        run: azd provision --no-prompt
        env:
          AZURE_ENV_NAME: ${{ vars.AZURE_ENV_NAME }}
          AZURE_LOCATION: ${{ vars.AZURE_LOCATION }}
          AZURE_SUBSCRIPTION_ID: ${{ vars.AZURE_SUBSCRIPTION_ID }}
      - name: azd deploy
        run: azd deploy --no-prompt
        env:
          AZURE_ENV_NAME: ${{ vars.AZURE_ENV_NAME }}
      - name: Export endpoints
        id: endpoints
        run: |
          azd env get-values > .env.azd
          # Adjust the key names below to match your bicep/terraform outputs
          FRONTEND_URL="$(grep -E '^SERVICE_WEB_URI=' .env.azd | cut -d= -f2- | tr -d '"')"
          BACKEND_URL="$(grep -E '^SERVICE_API_URI=' .env.azd | cut -d= -f2- | tr -d '"')"
          echo "frontend_url=$FRONTEND_URL" >> "$GITHUB_OUTPUT"
          echo "backend_url=$BACKEND_URL"   >> "$GITHUB_OUTPUT"
        env:
          AZURE_ENV_NAME: ${{ vars.AZURE_ENV_NAME }}

  validate-deployment:
    needs: deploy
    runs-on: ubuntu-latest
    steps:
      - name: Probe health endpoints
        run: |
          set -euo pipefail
          probe() {
            local base="$1"
            [ -z "$base" ] && { echo "::error::empty URL"; exit 1; }
            for path in /health /healthz /api/health /; do
              code="$(curl -fsS -L -o /dev/null -w '%{http_code}' --max-time 15 --retry 5 --retry-all-errors --retry-delay 6 "$base$path" || echo 000)"
              echo "  $base$path -> $code"
              if [ "$code" = "200" ]; then return 0; fi
            done
            echo "::error::no 2xx from $base"
            return 1
          }
          probe "${{ needs.deploy.outputs.frontend_url }}"
          probe "${{ needs.deploy.outputs.backend_url }}"

  test-backend:
    needs: [deploy, validate-deployment]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # --- Replace this block with the framework detected in Prerequisite #6 ---
      - name: Set up Python
        uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - name: Install backend deps
        working-directory: src/api
        run: pip install -r requirements.txt -r requirements-dev.txt
      - name: Run backend tests
        working-directory: src/api
        env:
          BACKEND_URL: ${{ needs.deploy.outputs.backend_url }}
        run: pytest -q --maxfail=1 --junitxml=backend-results.xml
      - name: Upload backend test results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: backend-test-results
          path: src/api/backend-results.xml

  test-frontend-playwright:
    needs: [deploy, validate-deployment]
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: src/web   # adjust to your frontend project dir
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'npm'
          cache-dependency-path: src/web/package-lock.json
      - name: Install dependencies
        run: npm ci
      - name: Install Playwright browsers
        run: npx playwright install --with-deps
      - name: Run Playwright tests against deployed URL
        env:
          PLAYWRIGHT_BASE_URL: ${{ needs.deploy.outputs.frontend_url }}
          BASE_URL:            ${{ needs.deploy.outputs.frontend_url }}
        run: npx playwright test --reporter=line,html
      - name: Upload Playwright report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: playwright-report
          path: |
            src/web/playwright-report/
            src/web/test-results/
          retention-days: 14

  release-gate:
    needs: [deploy, validate-deployment, test-backend, test-frontend-playwright]
    runs-on: ubuntu-latest
    steps:
      - name: All checks passed
        run: echo "Deployment + tests succeeded. Release verified."
```

> **Pin versions to current.** Verify the latest `Azure/setup-azd`, `actions/checkout`, `actions/setup-node`, `actions/setup-python`, and `actions/upload-artifact` tags at run time; pin to a specific SHA for supply-chain-sensitive environments.

> **Key names matter.** `SERVICE_WEB_URI` / `SERVICE_API_URI` are conventions, not guarantees. Run `azd env get-values` locally and update the `grep -E` lines to whatever your infra actually outputs. Step 6 of this skill does the same locally — keep both in sync.

> **No frontend? No backend?** Delete the corresponding job. Don't leave stub jobs that always pass — they hide regressions.

## Failure recovery

| Failure | Recovery |
|---|---|
| `azd pipeline config` fails on SP creation | User lacks AAD permissions. Confirm role (`Application Developer` or higher) via `ask_user`. Offer manual `az ad sp create-for-rbac` flow as fallback. |
| `azd pipeline config` fails on federated credential | Often a tenant policy issue. Show full error; do NOT retry blindly. `ask_user` whether to switch to client-credentials. |
| `gh auth status` fails | Run `gh auth login --web --scopes "repo,workflow,admin:org"`. Do NOT proceed without user auth. |
| Push rejected (non-fast-forward) | `git fetch && git status`. `ask_user` before any rebase or force operation. |
| Workflow run fails: "AADSTS70021" | Federated credential subject mismatch. Run `az ad app federated-credential list --id $AZURE_CLIENT_ID` and compare to the failing run's `sub` claim. |
| Workflow run fails: insufficient role | The SP needs `Contributor` + `User Access Administrator` (or fine-grained equivalents) on the target scope. Show the `az role assignment` command; do NOT execute without confirmation. |
| Workflow run fails: region quota | Surface the Azure error verbatim; offer to switch `AZURE_LOCATION` via `azd env set`. |
| `gh run watch --exit-status` exits non-zero | Pipeline itself failed. Run `gh run view "$RUN_ID" --log-failed`, identify the failing job, and route by job name (deploy → provision/role error; validate-deployment → endpoint not healthy; test-backend → backend regression; test-frontend-playwright → UI regression). Do NOT auto-retry; surface to user. |
| `validate-deployment` job fails (no 2xx) | Likely cold start, missing route, or app crashed. Locally run `azd env get-values` + `curl -v`. For cold-start services (Container Apps, Functions consumption), `ask_user` whether to retry after 60-120s warm-up. For 5xx, pull container logs (`az containerapp logs show`, `az webapp log tail`) and surface. |
| Playwright job fails: `Executable doesn't exist` | Missing browsers. Ensure `npx playwright install --with-deps` ran. On self-hosted runners without root, switch to a container image that bundles browsers (e.g., `mcr.microsoft.com/playwright:v1.xx.x-jammy`). |
| Playwright job fails: every test times out | Almost always wrong `PLAYWRIGHT_BASE_URL`. Confirm the `deploy` job's `endpoints` output matches the actual deployed hostname (not `localhost`, not the staging slot). |
| Playwright job fails: real assertion failures | This is a regression in the deployed app, not a flake. Download the `playwright-report` artifact, surface the failing spec name and `expect()` diff to the user. `ask_user` whether to roll back (`azd provision` on the previous commit), open a hotfix branch, or accept the failure. Never silently mark the release green. |
| Backend test job fails | Same handling as Playwright real failures — treat as a regression. Surface failing test names + first stack trace, offer rollback / hotfix path, do NOT auto-retry. |
| Backend / Playwright tests pass locally but fail in workflow | Almost always env-var mismatch (the workflow's `BACKEND_URL` / `PLAYWRIGHT_BASE_URL` differ from what your local run used). Compare `azd env get-values` output to the workflow's `endpoints` step output. |

## Handoff

After Step 7, this skill is complete. No mandatory downstream skill, but suggest to the user:

- Configure GitHub Environment protection rules on `production` (required reviewers, wait timer)
- Add branch protection on `main` (required PR review, **make `validate-deployment`, `test-backend`, and `test-frontend-playwright` required status checks**)
- Add a `staging` environment with its own `AZURE_ENV_NAME` and federated credential subject
- If using Terraform, configure remote state in Azure Storage (typically handled by `azure-prepare`)
- If the Playwright suite is a single smoke test scaffolded during Prerequisite #6, file a follow-up to expand coverage (auth flows, critical user journeys, mobile viewport)

## Out of scope

- Azure DevOps Pipelines — use `azd pipeline config --provider azdo`
- Multi-cloud deployments
- Container image build/push beyond what `azd deploy` orchestrates
- Terraform remote state setup — belongs in `azure-prepare`
- Secret rotation automation for client-credentials SPs — out of scope by design; OIDC removes the need
- **Authoring** backend or Playwright tests beyond a single smoke spec — this skill runs existing tests and gates the release on them, but it does not invent coverage. For Playwright authoring help, compose with the `playwright-cli` / `playwright-dev` skills.
- Load / performance / security testing — out of scope. A green release here means functional correctness on the smoke-to-suite level, not capacity.

---

**Installation note:** This skill lives at `~/.copilot/skills/release/SKILL.md` (Copilot CLI user-scope). To distribute it alongside microsoft/azure-skills, copy to `.github/plugins/<your-org>/skills/release/SKILL.md`; for Claude Code, copy to `.claude/skills/release/SKILL.md`. Reference it from your repo `AGENTS.md` / `CLAUDE.md` so the harness auto-discovers it.