# long-running-agent

Persistent Zellij terminals and a private VM browser over Google IAP and OS Login.
Disconnecting leaves your terminals and browser running. Your profiles,
credentials, browser sessions and conversations stay outside this repository.

Install [uv](https://docs.astral.sh/uv/) and the
[Google Cloud CLI](https://cloud.google.com/sdk/docs/install), then:

```bash
uv sync --locked
uv tool install --editable ./packages/long-running-agent
```

Create your personal `~/.config/long-running-agent/config.toml`:

```toml
account = "you@example.com"
project = "your-project-id"
instance = "your-vm"
zone = "us-central1-a"
```

Then run `gcloud auth login you@example.com` and `lra vm ssh-config`.
CLI options override `LRA_*` environment variables, then your profile.

```bash
vm                             # Reattach your persistent terminal
vm terminal project-work       # Select another Zellij session
vm browser https://example.com # Open the existing VM browser
vm send ~/Desktop/image.png --copy-path
vm get /home/YOUR_USER/code/project/result.png
vm codex switch                # Native account sign-in; reopens saved panes
vm --help                      # Connection and power commands
lra runtime --help             # Setup, diagnostics, cleanup and maintenance
```

`sarj-connect` provides the same terminal/browser connections; `sarj-vm` aliases
`vm`. **Option-D** detaches. Checkpoint conversations and run
`lra runtime worktrees preserve` before stopping or rebooting the VM.
Account switching interrupts turns; unpersisted input is lost.

Browser access requires an administrator-prepared persistent Chrome service
and compatible `sarj-browser` helper. `vm browser --no-open` prints the private
viewer address; keep that connection open. Use the viewer's Clipboard panel
for passwords. Native Codex and Claude keep their own accounts and settings.

From a VM terminal connected from your Mac, `sarj-host browser URL`,
`sarj-host image` and `sarj-host download PATH` request actions on that Mac.
Transfers require explicit selection/confirmation, verify size and SHA-256,
and preserve existing files. See `sarj-host --help` for connection selection.

An administrator runs `lra runtime admin prepare-host` once. Each VM user
installs the package, then runs `lra runtime setup` and `lra runtime doctor`.
Diagnosis checks actual sign-in; Google CLI and ADC credentials are separate.

The Python API exposes `Client` and `Settings`:

```python
from long_running_agent import Client, Settings

client = Client()  # Load your profile; construction does not connect
client.terminal()
client.browser(url="https://example.com")
```

Terraform composes host, access and backup modules with protected state and
explicit deployment inputs. Copy `terraform/example.tfvars` to
`terraform/private/deployment.tfvars` and `terraform/example.backend.hcl` to
`terraform/private/backend.hcl`, then replace every placeholder. Preserve exact
existing deployment values during adoption; moved blocks preserve addresses.

```bash
lra infra tf init -backend-config=private/backend.hcl -lockfile=readonly
lra infra tf plan -var-file=private/deployment.tfvars -out=private/deployment.tfplan
lra infra tf show private/deployment.tfplan
lra infra tf apply private/deployment.tfplan
```

Review the saved plan before applying; resizing requires an attended shutdown.
Import existing IAP audit configuration before adopting it. Terraform does not
manage the VM's power state. Keep deployment values, state and plans private.

Develop with `uv sync --locked`, `uv python install 3.12`, and
`uv run --locked pytest -n 2 --dist worksteal`. The application uses Python 3.14;
isolated host workers also support Python 3.12. Canonical hooks and CI run Code
Standards; dedicated CI scans source and history for secrets.

Licensed under [MIT](LICENSE). Command help owns the detailed reference.
