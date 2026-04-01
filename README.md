# data-ingestion-pydata-helenski-demo

## Presentation

[PyData Helsinki Demo — Google Slides](https://docs.google.com/presentation/d/1iWNqe-PDDbrXpOal0TMOAvmb7SS7AzQsf6DyEvkgFMI/edit?usp=sharing)

## Photos

[![PyData Helsinki Photo 1](https://www.meetup.com/pydatahelsinki/photos/35854184/532932617/)](https://www.meetup.com/pydatahelsinki/photos/35854184/532932617/)

[![PyData Helsinki Photo 2](https://www.meetup.com/pydatahelsinki/photos/35854184/532932618/)](https://www.meetup.com/pydatahelsinki/photos/35854184/532932618/)

## Python Backend (First-Time User Guide)

If you are new, this guide is for you.

Goal: you can type a request like **"connect to notion"** or **"connect to linear"** and use the CLI step by step.

## What this tool gives you

- `agentctl`: create and manage connections (“sources”).
- `agentctl chat --cli ...`: consolidated chat-first command for connect + actions (recommended).

## 10-minute first run (copy/paste)

### Step 1: Install

```bash
uv sync --project python
uv tool install --editable ./python
```

Check installation:

```bash
agentctl --help
```

### Step 2: Sign in once (Copilot CLI)

```bash
copilot login
```

Optional: if you still want `agentctl` local auth metadata, you can also run:

```bash
agentctl auth login --provider github-copilot --from-gh
```

### Step 3: Ask in plain English (your exact use case)

Example: “connect to linear” (chat-first)

```bash
agentctl chat --cli "connect to linear"
```

Single-command onboarding (connect + guided auth prompt):

```bash
agentctl chat --cli "connect to linear" --auto-auth
```

When docs are shown during `--auto-auth`, the CLI asks you to confirm whether those links are correct for your setup. If you answer no, onboarding does **not** stop: it runs an agentic docs probe to suggest refined authentication links, then continues credential onboarding.

Any tool with provider-type hint (API or MCP), still chat-based:

```bash
agentctl chat --cli "connect to zendesk" --provider-type api
agentctl chat --cli "connect to github" --provider-type mcp
```

This performs the actual connection setup (creates the source config), not just a suggestion.

Important: `connect` does **not** auto-mark a source as connected when auth is required. It now returns authentication `nextSteps` and keeps status as `needs_auth` until you authenticate.

URL discovery is automatic. You do not need to pass endpoint URLs for connect in normal use.

You will see a step-by-step reasoning log during the run, for example:

- request received
- provider parsed
- preset/default selected
- source created

Safe mode (see request without sending):

```bash
agentctl chat --cli "connect to linear" --dry-run
```

### Step 4: Confirm your connection was created

```bash
agentctl list --workspace ~/.agent-runtime/workspaces/default
```

If your source name was `connect to linear`, the slug is usually `connect-to-linear`.

Check one source:

```bash
agentctl get --workspace ~/.agent-runtime/workspaces/default connect-to-linear
```

When auth is required, the source will show:

- `isAuthenticated: false`
- `connectionStatus: "needs_auth"`

This is expected until you complete authentication.

### Step 5: Mark it connected after auth is complete

Authentication flow (in order):

1. `connect` creates the source and returns `authentication.nextSteps`.
2. `credential set` stores your token/credential (or `--auto-auth` does this inline).
3. `mark-authenticated` sets `isAuthenticated=true` and `connectionStatus=connected`.

```bash
agentctl mark-authenticated --workspace ~/.agent-runtime/workspaces/default connect-to-linear
```

### Step 6: Health check

```bash
agentctl doctor --workspace ~/.agent-runtime/workspaces/default
```

## Do real actions after connecting (CLI only)

Once connected, you can ask the CLI to perform actual tool actions (not suggestions).

Current support in this version:

- **Any API tool** through agent-planned generic API calls.
- **Linear enhanced mode** with issue-aware actions (`list_issues`, `create_issue`).
- **MCP sources** via chat planning output (transport-aware MCP action plan) with live tool discovery.

MCP probing modes:

- `--mcp-probe live` (default): call MCP `tools/list` and use discovered tools for planning/tool selection.
- `--mcp-probe cached`: use static `mcp.tools` from source config.
- `--mcp-probe off`: skip probing.

### Step 7: Save credentials for the connected source

Example for Linear source slug `connect-to-linear`:

```bash
agentctl credential set \
	--workspace ~/.agent-runtime/workspaces/default \
	--source connect-to-linear
```

This now opens an interactive, secure prompt (hidden input) and shows provider auth guidance before asking for your token.

During auto-auth, you can explicitly reject the suggested docs when prompted (`Are these documentation links correct for your setup?`). If you reject them, the CLI probes for improved docs and continues with credential capture.

If you use `--auto-auth` at connect time, this step is done inline as part of the connect command.

Non-interactive/CI mode is still supported:

```bash
agentctl credential set \
	--workspace ~/.agent-runtime/workspaces/default \
	--source connect-to-linear \
	--value "YOUR_LINEAR_API_TOKEN" \
	--mark-authenticated
```

Non-interactive single-command onboarding:

```bash
agentctl chat --cli "connect to linear" \
	--auto-auth \
	--auth-value "YOUR_LINEAR_API_TOKEN" \
	--mark-authenticated
```

Check credential status:

```bash
agentctl credential status \
	--workspace ~/.agent-runtime/workspaces/default \
	--source connect-to-linear
```

`credential set`, `auth login`, and `mark-authenticated` now return a `steps` array in output so you can see each authentication step performed.

You can also view the auth flow + provider docs for a source at any time:

```bash
agentctl auth guide \
	--workspace ~/.agent-runtime/workspaces/default \
	--source connect-to-linear \
	--pretty
```

Example `steps` output (shortened):

```json
{
	"steps": [
		"Resolved workspace: ...",
		"Looking up source: connect-to-linear",
		"Source found.",
		"Stored credential cache with TTL=24h."
	]
}
```

### Step 8: Execute actions with agentic reasoning

List issues in Linear:

```bash
agentctl chat --cli "list all issues in linear" \
	--workspace ~/.agent-runtime/workspaces/default
```

Run a non-Linear tool action (example: Zendesk tickets):

```bash
agentctl chat --cli "list tickets from /tickets.json in zendesk" \
	--workspace ~/.agent-runtime/workspaces/default
```

Create a Linear issue:

```bash
agentctl chat --cli "create issue in linear in team ENG titled Fix login bug description OAuth callback fails" \
	--workspace ~/.agent-runtime/workspaces/default
```

Plan-only mode (no API execution):

```bash
agentctl chat --cli "create issue in linear titled Fix login bug" \
	--workspace ~/.agent-runtime/workspaces/default \
	--dry-run
```

The output includes `agentReasoning`, which explains how the agent interpreted your request and why it chose each action.

If an action fails, the agent attempts a self-healing step (safe retry plan) and reports what it changed.

For API actions, if an authorization failure occurs, the runner now attempts auth types automatically (`bearer`, `basic`, `header`, `query`, `none`) and uses the first one that succeeds.

When running with interactive fix enabled, auth failures also offer retry options to choose one auth type or retry all auth types explicitly.

When running in a terminal (TTY), failures can open an **interactive fix assistant** that offers quick recovery actions (for example: switch MCP probe mode, increase timeout, or update credentials) and can retry automatically.

If you want guarded patch-style remediation, enable `--guarded-auto-apply`: the CLI shows a diff preview for supported safe fixes, asks for explicit confirmation, then applies and retries.

For broader policy control, use `--fix-mode`, `--fix-scope`, and `--allow-code-patch`. Current shipped code-patch handler can propose updating CLI timeout defaults when timeout-class errors recur.

By default, `chat`, `connect`, `act`, and `copilot` flows now stream real-time progress to **stderr** as JSON events (`step`, `reasoning`, `error`, `self_heal`, `fix`, `stream`) while final result JSON is still printed to **stdout**.

If you need quiet/non-stream mode:

```bash
agentctl chat --cli "list all issues in linear" --no-stream
```

## If you want a manual command (no AI suggestion)

Use this template and replace values in ALL CAPS:

```bash
agentctl create \
	--workspace ~/.agent-runtime/workspaces/default \
	--name "connect to TOOL_NAME" \
	--type api \
	--provider TOOL_NAME \
	--api '{"baseUrl":"https://API_BASE_URL","authType":"bearer"}'
```

Example:

```bash
agentctl create \
	--workspace ~/.agent-runtime/workspaces/default \
	--name "connect to linear" \
	--type api \
	--provider linear \
	--api '{"baseUrl":"https://api.linear.app","authType":"bearer"}'
```

## Most useful everyday commands

```bash
agentctl chat --cli "connect to notion"
agentctl chat --cli "list all issues in linear"
agentctl list --workspace ~/.agent-runtime/workspaces/default
agentctl get --workspace ~/.agent-runtime/workspaces/default SOURCE_SLUG
agentctl delete --workspace ~/.agent-runtime/workspaces/default SOURCE_SLUG
agentctl credential status --workspace ~/.agent-runtime/workspaces/default --source SOURCE_SLUG
agentctl act "list all issues in linear" --workspace ~/.agent-runtime/workspaces/default --source SOURCE_SLUG
agentctl auth status
agentctl auth logout
```

## Optional advanced runtime notes

`session-mcp-server` and `bridge-mcp-server` are internal runtime services used for session orchestration and API/MCP bridging.

For normal usage, run the consolidated command instead:

```bash
agentctl chat --cli "connect to linear"
agentctl chat --cli "list all issues in linear"
```

## Configuration reference (`agentctl chat --cli`)

| Flag | Purpose | Example |
|---|---|---|
| `--provider-type {auto\|api\|mcp}` | Hint connect discovery to prefer API or MCP sources. | `agentctl chat --cli "connect to github" --provider-type mcp` |
| `--source SOURCE_SLUG` | Force which connected source to use for action requests. | `agentctl chat --cli "list tickets" --source connect-to-zendesk` |
| `--workspace PATH` | Use a non-default workspace root. | `agentctl chat --cli "connect to linear" --workspace ~/.agent-runtime/workspaces/default` |
| `--dry-run` | Show the inferred plan/reasoning without executing actions. | `agentctl chat --cli "create issue in linear titled Fix login bug" --dry-run` |
| `--heal-attempts N` | Retry failed actions with self-healing plan patches. | `agentctl chat --cli "list tickets from /tickets.json in zendesk" --heal-attempts 3` |
| `--mcp-probe {live\|cached\|off}` | MCP tool discovery mode for action requests. | `agentctl chat --cli "list available tools in github" --mcp-probe live` |
| `--interactive-fix` / `--no-interactive-fix` | Enable/disable interactive failure recovery assistant (TTY only). Default is on. | `agentctl chat --cli "list available tools in github" --no-interactive-fix` |
| `--guarded-auto-apply` / `--no-guarded-auto-apply` | Preview safe fix diffs and apply only after explicit confirmation, then retry. Default is off. | `agentctl chat --cli "list all issues in linear" --guarded-auto-apply` |
| `--fix-mode {suggest\|guarded\|auto}` | Set remediation behavior on failures: suggest only, confirm-before-apply, or auto-apply for supported fixes. | `agentctl chat --cli "list all issues in linear" --fix-mode guarded` |
| `--fix-scope {runtime\|config\|code\|all}` | Constrain which fix categories can be auto-applied. | `agentctl chat --cli "list all issues in linear" --fix-mode guarded --fix-scope config` |
| `--allow-code-patch` / `--no-allow-code-patch` | Allow code-file patch fixes when a supported handler matches (example shipped: timeout default patch in CLI). | `agentctl chat --cli "list available tools in github" --fix-mode guarded --fix-scope code --allow-code-patch` |
| `--fix-dry-run` / `--no-fix-dry-run` | Show proposed fix diff without applying changes. | `agentctl chat --cli "list all issues in linear" --fix-mode guarded --fix-scope config --fix-dry-run` |
| `--auth-types-try CSV` | Override auth fallback order for API auth failures (comma-separated: `bearer,basic,header,query,none`). | `agentctl chat --cli "list tickets" --auth-types-try header,query,none` |
| `--timeout SECONDS` | Planner/discovery timeout. | `agentctl chat --cli "connect to notion" --timeout 90` |
| `--stream` / `--no-stream` | Toggle real-time stderr event streaming for steps/errors/retries. Default is on. | `agentctl chat --cli "list all issues in linear" --no-stream` |
| `--base-url URL` | Override discovered base URL on connect requests. | `agentctl chat --cli "connect to zendesk" --provider-type api --base-url https://subdomain.zendesk.com/api/v2` |
| `--auth-type TYPE` | Override discovered auth type on connect requests. | `agentctl chat --cli "connect to zendesk" --provider-type api --auth-type bearer` |
| `--auto-auth` | Start guided authentication immediately after connect (single-command onboarding). | `agentctl chat --cli "connect to linear" --auto-auth` |
| `--auth-value VALUE` | Provide credential for non-interactive auto-auth flow. | `agentctl chat --cli "connect to linear" --auto-auth --auth-value "TOKEN"` |
| `--mark-authenticated` | Mark source connected after successful credential capture. | `agentctl chat --cli "connect to linear" --auto-auth --auth-value "TOKEN" --mark-authenticated` |

## Files this creates

- `~/.agent-runtime/auth/github-copilot.json`
- `~/.agent-runtime/workspaces/default/sources/<source-slug>/config.json`
- `~/.agent-runtime/workspaces/default/sources/<source-slug>/guide.md`

## Optional environment settings

```bash
export AGENT_COPILOT_MODEL="gpt-5.3-codex"
```

Note: `agentctl copilot ...` now uses local `copilot` CLI execution (not direct endpoint calls).

## Troubleshooting (common issues)

### 1) `copilot` login fails or `copilot` is not found

Cause: Copilot CLI is not installed or not authenticated yet.

Fix:

```bash
copilot --help
copilot login
```

If you use `agentctl auth login --from-gh`, make sure `gh` is logged in:

```bash
gh auth login
```

### 2) `agentctl copilot ...` fails with auth/session message

Cause: Copilot CLI session is not authenticated.

Fix:

```bash
copilot login
```

### 2b) I want to hide reasoning logs

By default, `agentctl connect ...` shows reasoning logs.

To disable:

```bash
agentctl connect "connect to linear" --no-show-reasoning
```

### 3) `Source not found` when running `get`, `delete`, or `mark-authenticated`

Cause: wrong source slug or wrong workspace path.

Fix:

```bash
agentctl list --workspace ~/.agent-runtime/workspaces/default
```

Copy the exact `slug` from the list, then run the command again with that slug.

### 3b) `agentctl act ...` says credentials are missing

Cause: source has no credential cache yet.

Fix:

```bash
agentctl credential set --workspace ~/.agent-runtime/workspaces/default --source connect-to-linear
```

If you want to check the exact provider flow first:

```bash
agentctl auth guide --workspace ~/.agent-runtime/workspaces/default --source connect-to-linear --pretty
```

### 3c) Chat chose the wrong source

If you have many connected tools and the request is ambiguous, pass source explicitly:

```bash
agentctl chat --cli "list tickets" --source connect-to-zendesk
```

### 4) Start fresh (safe reset)

If you are testing and want a clean state:

```bash
rm -rf ~/.agent-runtime/workspaces/default/sources
mkdir -p ~/.agent-runtime/workspaces/default/sources
```

## Need deeper architecture details?

See the repository root README for full internals and diagrams.
