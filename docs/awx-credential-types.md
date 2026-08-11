# AWX Credential Types and Injection into Ansible Runner

This document describes how credential types are defined in AWX and how
credentials of those types are ultimately provided to Ansible via
ansible-runner.

## Architecture Overview

The credential system spans three repositories:

| Repository | Package | Purpose |
|---|---|---|
| [awx](https://github.com/ansible/awx) | `awx` | `CredentialType`/`Credential` Django models, the job task runner that orchestrates injection and invokes ansible-runner |
| [awx-plugins](https://github.com/ansible/awx-plugins) | `awx-plugins-core` | All managed credential type definitions (`inputs`, `injectors`, `custom_injectors`) and Python injector functions |
| [awx_plugins.interfaces](https://github.com/ansible/awx_plugins.interfaces) | `awx-plugins-interfaces` | The `inject_credential()` engine that renders Jinja2 templates, writes files, and sets env vars |

## Credential Type Model

Each `CredentialType` (defined in `awx/main/models/credential.py`) has:

- **`kind`** &mdash; Categorisation: `ssh`, `vault`, `net`, `scm`, `cloud`,
  `registry`, `token`, `insights`, `external`, `kubernetes`, `galaxy`,
  `cryptography`.
- **`managed`** &mdash; `True` for built-in types, `False` for user-created.
- **`namespace`** &mdash; Unique string identifier for managed types (e.g.
  `"ssh"`, `"aws"`, `"gce"`).
- **`inputs`** &mdash; JSON schema defining the credential's fields.
- **`injectors`** &mdash; JSON defining template-based injection into env vars,
  files, and/or extra vars.
- **`custom_injectors`** &mdash; Optional Python callable for complex injection
  logic (not a DB field, only available on managed types).

### Inputs Schema

The `inputs` field defines what data a credential stores:

```json
{
  "fields": [
    {
      "id": "username",
      "label": "Username",
      "type": "string"
    },
    {
      "id": "password",
      "label": "Password",
      "type": "string",
      "secret": true
    }
  ],
  "required": ["username"]
}
```

Each field supports these properties:

| Property | Type | Description |
|---|---|---|
| `id` | string | Variable name identifier (required) |
| `label` | string | Display label (required) |
| `type` | `"string"` or `"boolean"` | Field data type (default: `"string"`) |
| `secret` | boolean | Encrypted at rest, masked in API responses |
| `multiline` | boolean | Hint for multi-line text input (e.g. SSH keys) |
| `format` | `"ssh_private_key"` or `"url"` | Triggers format-specific validation |
| `choices` | string[] | Restricts value to an enumerated list |
| `ask_at_runtime` | boolean | Allow prompting at job launch (managed types only) |
| `default` | string or boolean | Default value matching the declared type |
| `help_text` | string | Help text shown in the UI |

### Injectors Schema

The `injectors` field defines how credential values are injected at runtime.
All values are Jinja2 templates rendered against the credential's input fields:

```json
{
  "env": {
    "MY_TOKEN": "{{api_token}}"
  },
  "file": {
    "template": "config content with {{secret}}"
  },
  "extra_vars": {
    "my_var": "{{api_token}}"
  }
}
```

| Key | Description |
|---|---|
| `env` | Sets environment variables on the ansible-playbook process |
| `file` | Writes temp files; path available as `{{tower.filename}}` (single) or `{{tower.filename.<name>}}` (multiple via `template.<name>` keys) |
| `extra_vars` | Writes a YAML extra vars file, passed to ansible-playbook as `-e @<path>` |

Restrictions on `env`:
- Cannot set `ANSIBLE_*` prefixed variables.
- Cannot set blocklisted variables (`PATH`, `PYTHONPATH`, `VIRTUAL_ENV`,
  `JOB_ID`, etc.).

### Managed Type Discovery

Managed credential types are not defined in AWX itself. They are discovered at
runtime from Python entry points registered by the `awx-plugins-core` package:

- `awx_plugins.managed_credentials` &mdash; loaded for AWX deployments.
- `awx_plugins.managed_credentials.supported` &mdash; additionally loaded for
  downstream (non-AWX) products.

The types are stored in `ManagedCredentialType.registry`, a `LazyLoadDict` that
populates on first access. The database stores `inputs={}` and `injectors={}`
for managed types; the real values are overlaid from the in-memory registry at
read time via `CredentialType.from_db()`.

## Injection Pipeline

When a job runs, `BaseTask.run()` in `awx/main/tasks/jobs.py` assembles
credential data through these steps:

1. **`build_private_data_files()`** &mdash; Writes SSH key files to temp
   directory; returns raw `ssh_key_data`.
2. **`build_passwords()`** &mdash; Builds a dict of password values for pexpect.
3. **`build_extra_vars_file()`** &mdash; Writes the `env/extravars` file.
4. **`build_args()`** &mdash; Builds CLI args (`-u`, `--become`,
   `--ask-vault-pass`, etc.).
5. **`build_env()`** &mdash; Builds the base environment variable dict.
6. **Credential injection loop** &mdash; Iterates over all job credentials,
   calling `credential.credential_type.inject_credential()` which mutates the
   env dict, CLI args list, and writes files to the private data directory.
7. **Runner invocation** &mdash; Passes the assembled params to ansible-runner.

### What ansible-runner Receives

```python
params = {
    "private_data_dir": private_data_dir,  # temp files: keys, configs, extravars, cmdline
    "envvars": env,                         # all environment variables
    "ssh_key": ssh_key_data,                # raw SSH key string (runner manages ssh-agent)
    "passwords": expect_passwords,          # {regex_pattern: password_value} for pexpect
    "playbook": "...",
    "inventory": "...",
}
```

For non-SystemJob tasks, these params are passed to `AWXReceptorJob`, which uses
`ansible_runner.interface.run(streamer='transmit', ...)` to serialize and
transmit them to a remote execution node via Receptor.

## Injection Mechanisms

There are four distinct pathways credentials take to reach Ansible:

### 1. Hardcoded in the AWX Task Runner

The `ssh`, `vault`, `scm`, and `net` types have **no `injectors` and no
`custom_injectors`**. AWX's task runner handles them with explicit code in
`RunJob` and `RunProjectUpdate` (`awx/main/tasks/jobs.py`).

| Mechanism | How it reaches Ansible | Used by |
|---|---|---|
| SSH key via ssh-agent | Runner starts ssh-agent, loads key from `params["ssh_key"]` | Machine (`ssh`), SCM (`scm`), Network (`net`) |
| CLI args | Written to `env/cmdline` | Machine (`-u`, `--become`, `--become-method`, `--become-user`, `--ask-pass`, `--ask-become-pass`), Vault (`--ask-vault-pass`, `--vault-id <id>@prompt`) |
| pexpect password prompts | Runner watches stdout for regex patterns, sends password | SSH password, become password, vault password, key passphrase |
| Direct env vars in `build_env()` | Set on the ansible-playbook process | Network (`ANSIBLE_NET_*`) |

### 2. `custom_injectors` &mdash; Python Callable

These types define a Python function that receives
`(credential, env, private_data_dir)` and directly sets environment variables
and/or writes config files. Only available for managed types.

### 3. Template-based `injectors` JSON

These types define an `injectors` dict with `env`, `file`, and/or `extra_vars`
sections. The `inject_credential()` engine in `awx-plugins-interfaces` renders
all Jinja2 templates in a sandboxed environment.

### 4. Consumed by AWX Internally (Not Injected into Ansible)

Some credential types are consumed by AWX itself rather than being injected
into ansible-runner:

- **Galaxy (`galaxy_api_token`)** &mdash; Handled in
  `RunProjectUpdate.build_env()` to set `ANSIBLE_GALAXY_SERVER_*` env vars
  during project syncs.
- **Container Registry (`registry`)** &mdash; Used in
  `build_execution_environment_params()` to authenticate container image pulls
  via `container_auth_data`.
- **SCM tokens (`github_token`, `gitlab_token`, `bitbucket_dc_token`)** &mdash;
  Used for webhook status callbacks to SCM providers, not injected into
  playbook runs.
- **GPG Public Key (`gpg_public_key`)** &mdash; Passed as the `gpg_pubkey`
  extra var during project updates for content signature validation.

## Custom (User-Defined) Credential Types

Users can create custom credential types through the API. Custom types are
**limited to the three template-based injector mechanisms**:

1. **`env`** &mdash; Set environment variables.
2. **`file`** &mdash; Write temp files.
3. **`extra_vars`** &mdash; Inject Ansible extra variables.

Custom types **cannot**:

- Add CLI arguments to the ansible-playbook command.
- Use SSH-agent integration (the `ssh_key` param to runner is only populated
  for `ssh`/`scm`/`net` kind credentials).
- Register pexpect password prompt/response pairs.
- Use Python-based `custom_injectors` (the code explicitly checks
  `cred_type.managed` before calling it).
- Set `ANSIBLE_*` prefixed environment variables or blocklisted variables.

## Managed Credential Types Reference

### Machine (`ssh`)

Provides SSH authentication for connecting to remote hosts.

**Namespace:** `ssh` | **Kind:** `ssh` | **Entry point group:** `awx_plugins.managed_credentials`

**Injection method:** Hardcoded in task runner

| Input Field | Label | Type | Secret | Injection |
|---|---|---|---|---|
| `username` | Username | string | no | CLI arg: `-u <value>` (defaults to `root`) |
| `password` | Password | string | yes | pexpect: responds to `SSH password:` and `Password:` prompts |
| `ssh_key_data` | SSH Private Key | string (ssh_private_key) | yes | Written to temp file, loaded into ssh-agent via `params["ssh_key"]` |
| `ssh_public_key_data` | Signed SSH Certificate | string | yes | Written as `<key_file>-cert.pub` alongside the private key |
| `ssh_key_unlock` | Private Key Passphrase | string | yes | pexpect: responds to `Enter passphrase for` prompts |
| `become_method` | Privilege Escalation Method | string | no | CLI arg: `--become-method <value>` |
| `become_username` | Privilege Escalation Username | string | no | CLI arg: `--become-user <value>` |
| `become_password` | Privilege Escalation Password | string | yes | pexpect: responds to `<method> password` and `BECOME password` prompts |

---

### Source Control (`scm`)

Provides authentication for SCM (git) operations during project updates.

**Namespace:** `scm` | **Kind:** `scm` | **Entry point group:** `awx_plugins.managed_credentials`

**Injection method:** Hardcoded in task runner (`RunProjectUpdate`)

| Input Field | Label | Type | Secret | Injection |
|---|---|---|---|---|
| `username` | Username | string | no | pexpect: responds to username prompts during git operations |
| `password` | Password | string | yes | pexpect: responds to password prompts during git operations |
| `ssh_key_data` | SCM Private Key | string (ssh_private_key) | yes | Written to temp file, loaded into ssh-agent |
| `ssh_key_unlock` | Private Key Passphrase | string | yes | pexpect: responds to passphrase prompts |

---

### Vault (`vault`)

Provides Ansible Vault decryption passwords.

**Namespace:** `vault` | **Kind:** `vault` | **Entry point group:** `awx_plugins.managed_credentials`

**Injection method:** Hardcoded in task runner

| Input Field | Label | Type | Secret | Injection |
|---|---|---|---|---|
| `vault_password` | Vault Password | string | yes | CLI arg: `--ask-vault-pass` (no vault ID) or `--vault-id <id>@prompt` (with vault ID); pexpect responds to `Vault password:` prompts |
| `vault_id` | Vault Identifier | string | no | Used with `--vault-id` to support multiple vault passwords |

**Required:** `vault_password`

---

### Network (`net`)

Provides authentication for Ansible network modules.

**Namespace:** `net` | **Kind:** `net` | **Entry point group:** `awx_plugins.managed_credentials`

**Injection method:** Hardcoded in task runner (`build_env()`)

| Input Field | Label | Type | Secret | Injection |
|---|---|---|---|---|
| `username` | Username | string | no | Env: `ANSIBLE_NET_USERNAME` |
| `password` | Password | string | yes | Env: `ANSIBLE_NET_PASSWORD` |
| `ssh_key_data` | SSH Private Key | string (ssh_private_key) | yes | Written to temp file; env: `ANSIBLE_NET_SSH_KEYFILE` |
| `ssh_key_unlock` | Private Key Passphrase | string | yes | pexpect: responds to passphrase prompts |
| `authorize` | Authorize | boolean | no | Env: `ANSIBLE_NET_AUTHORIZE` (as `"0"` or `"1"`) |
| `authorize_password` | Authorize Password | string | yes | Env: `ANSIBLE_NET_AUTH_PASS` (only when `authorize` is true) |

**Required:** `username`
**Dependencies:** `authorize_password` depends on `authorize`

---

### Amazon Web Services (`aws`)

Provides authentication for AWS modules and inventory plugins.

**Namespace:** `aws` | **Kind:** `cloud` | **Entry point group:** `awx_plugins.managed_credentials.supported`

**Injection method:** `custom_injectors` Python function

| Input Field | Label | Type | Secret | Injection |
|---|---|---|---|---|
| `username` | Access Key | string | no | Env: `AWS_ACCESS_KEY_ID` |
| `password` | Secret Key | string | yes | Env: `AWS_SECRET_ACCESS_KEY` |
| `security_token` | STS Token | string | yes | Env: `AWS_SECURITY_TOKEN` and `AWS_SESSION_TOKEN` |

**Required:** `username`, `password`

---

### Google Compute Engine (`gce`)

Provides authentication for GCP modules and inventory plugins.

**Namespace:** `gce` | **Kind:** `cloud` | **Entry point group:** `awx_plugins.managed_credentials.supported`

**Injection method:** `custom_injectors` Python function

| Input Field | Label | Type | Secret | Injection |
|---|---|---|---|---|
| `username` | Service Account Email Address | string | no | Env: `GCE_EMAIL`; included in JSON service account file as `client_email` |
| `project` | Project | string | no | Env: `GCE_PROJECT`, `GCP_PROJECT`; included in JSON file as `project_id` |
| `ssh_key_data` | RSA Private Key | string (ssh_private_key) | yes | Included in JSON service account file as `private_key` |

**Required:** `username`, `ssh_key_data`

Writes a JSON service account file to a temp file and sets:
- `GCE_CREDENTIALS_FILE_PATH`
- `GCP_SERVICE_ACCOUNT_FILE`
- `GOOGLE_APPLICATION_CREDENTIALS`
- `GCP_AUTH_KIND` = `"serviceaccount"`
- `GCP_ENV_TYPE` = `"tower"`

---

### Microsoft Azure Resource Manager (`azure_rm`)

Provides authentication for Azure modules and inventory plugins.

**Namespace:** `azure_rm` | **Kind:** `cloud` | **Entry point group:** `awx_plugins.managed_credentials.supported`

**Injection method:** `custom_injectors` Python function

| Input Field | Label | Type | Secret | Injection |
|---|---|---|---|---|
| `subscription` | Subscription ID | string | no | Env: `AZURE_SUBSCRIPTION_ID` |
| `username` | Username | string | no | Env: `AZURE_AD_USER` (when no client/tenant) |
| `password` | Password | string | yes | Env: `AZURE_PASSWORD` (when no client/tenant) |
| `client` | Client ID | string | no | Env: `AZURE_CLIENT_ID` (service principal auth) |
| `secret` | Client Secret | string | yes | Env: `AZURE_SECRET` (service principal auth) |
| `tenant` | Tenant ID | string | no | Env: `AZURE_TENANT` (service principal auth) |
| `cloud_environment` | Azure Cloud Environment | string | no | Env: `AZURE_CLOUD_ENVIRONMENT` |

**Required:** `subscription`

Uses service principal auth (Client ID + Tenant + Secret) when both `client`
and `tenant` are provided, otherwise falls back to username/password auth.

---

### VMware vCenter (`vmware`)

Provides authentication for VMware modules and inventory plugins.

**Namespace:** `vmware` | **Kind:** `cloud` | **Entry point group:** `awx_plugins.managed_credentials.supported`

**Injection method:** `custom_injectors` Python function

| Input Field | Label | Type | Secret | Injection |
|---|---|---|---|---|
| `host` | VCenter Host | string | no | Env: `VMWARE_HOST` |
| `username` | Username | string | no | Env: `VMWARE_USER` |
| `password` | Password | string | yes | Env: `VMWARE_PASSWORD` |

**Required:** `host`, `username`, `password`

Also sets `VMWARE_VALIDATE_CERTS` from the AWX setting
`vmware_certificate_validation`.

---

### OpenStack (`openstack`)

Provides authentication for OpenStack modules and inventory plugins.

**Namespace:** `openstack` | **Kind:** `cloud` | **Entry point group:** `awx_plugins.managed_credentials.supported`

**Injection method:** `custom_injectors` Python function

| Input Field | Label | Type | Secret | Injection |
|---|---|---|---|---|
| `username` | Username | string | no | Included in `clouds.yaml` as `auth.username` |
| `password` | Password (API Key) | string | yes | Included in `clouds.yaml` as `auth.password` |
| `host` | Host (Authentication URL) | string | no | Included in `clouds.yaml` as `auth.auth_url` |
| `project` | Project (Tenant Name) | string | no | Included in `clouds.yaml` as `auth.project_name` |
| `project_domain_name` | Project (Domain Name) | string | no | Included in `clouds.yaml` as `auth.project_domain_name` |
| `domain` | Domain Name | string | no | Included in `clouds.yaml` as `auth.domain_name` |
| `region` | Region Name | string | no | Included in `clouds.yaml` as `region_name` |
| `verify_ssl` | Verify SSL | boolean | no | Included in `clouds.yaml` as `verify` (default: `true`) |

**Required:** `username`, `password`, `host`, `project`

Writes a `clouds.yaml` file to a temp file and sets `OS_CLIENT_CONFIG_FILE`.

---

### Red Hat Satellite 6 (`satellite6`)

Provides authentication for Red Hat Satellite 6 inventory plugins.

**Namespace:** `satellite6` | **Kind:** `cloud` | **Entry point group:** `awx_plugins.managed_credentials.supported`

**Injection method:** None (no `injectors` or `custom_injectors` defined;
consumed by inventory source injectors separately)

| Input Field | Label | Type | Secret |
|---|---|---|---|
| `host` | Satellite 6 URL | string | no |
| `username` | Username | string | no |
| `password` | Password | string | yes |

**Required:** `host`, `username`, `password`

---

### Red Hat Virtualization (`rhv`)

Provides authentication for oVirt/RHV modules and inventory plugins.

**Namespace:** `rhv` | **Kind:** `cloud` | **Entry point group:** `awx_plugins.managed_credentials.supported`

**Injection method:** Template-based `injectors`

| Input Field | Label | Type | Secret | Injection |
|---|---|---|---|---|
| `host` | Host (Authentication URL) | string | no | Env: `OVIRT_URL`; included in INI file |
| `username` | Username | string | no | Env: `OVIRT_USERNAME`; included in INI file |
| `password` | Password | string | yes | Env: `OVIRT_PASSWORD`; included in INI file |
| `ca_file` | CA File | string | no | Included in INI file (optional) |

**Required:** `host`, `username`, `password`

Writes an oVirt INI config file to a temp file and sets `OVIRT_INI_PATH` to its
path. Also sets `OVIRT_URL`, `OVIRT_USERNAME`, and `OVIRT_PASSWORD` as
environment variables.

---

### Red Hat Ansible Automation Platform (`controller`)

Provides authentication for the `ansible.controller` and `awx.awx` collection
modules.

**Namespace:** `controller` | **Kind:** `cloud` | **Entry point group:** `awx_plugins.managed_credentials`

**Injection method:** Template-based `injectors`

| Input Field | Label | Type | Secret | Injection |
|---|---|---|---|---|
| `host` | Red Hat Ansible Automation Platform | string | no | Env: `TOWER_HOST`, `CONTROLLER_HOST`, `AAP_HOSTNAME`; extra var: `aap_hostname` |
| `username` | Username | string | no | Env: `TOWER_USERNAME`, `CONTROLLER_USERNAME`, `AAP_USERNAME`; extra var: `aap_username` |
| `password` | Password | string | yes | Env: `TOWER_PASSWORD`, `CONTROLLER_PASSWORD`, `AAP_PASSWORD`; extra var: `aap_password` |
| `oauth_token` | OAuth Token | string | yes | Env: `TOWER_OAUTH_TOKEN`, `CONTROLLER_OAUTH_TOKEN`, `AAP_TOKEN`; extra var: `aap_token` |
| `verify_ssl` | Verify SSL | boolean | no | Env: `TOWER_VERIFY_SSL`, `CONTROLLER_VERIFY_SSL`, `AAP_VALIDATE_CERTS`; extra var: `aap_validate_certs` |
| `request_timeout` | Request Timeout | string | no | Env: `CONTROLLER_REQUEST_TIMEOUT`, `AAP_REQUEST_TIMEOUT`; extra var: `aap_request_timeout` (default: `"10"`) |

**Required:** `host`

Sets environment variables for three naming conventions (`TOWER_*`,
`CONTROLLER_*`, `AAP_*`) and corresponding extra vars for maximum
compatibility.

---

### Insights (`insights`)

Provides authentication for Red Hat Insights.

**Namespace:** `insights` | **Kind:** `insights` | **Entry point group:** `awx_plugins.managed_credentials.supported`

**Injection method:** Template-based `injectors`

| Input Field | Label | Type | Secret | Injection |
|---|---|---|---|---|
| `username` | Username | string | no | Env: `INSIGHTS_USER`; extra var: `scm_username` |
| `password` | Password | string | yes | Env: `INSIGHTS_PASSWORD`; extra var: `scm_password` |
| `client_id` | Client ID | string | no | Env: `INSIGHTS_CLIENT_ID`; extra var: `client_id` |
| `client_secret` | Client Secret | string | yes | Env: `INSIGHTS_CLIENT_SECRET`; extra var: `client_secret` |

Also injects an `authentication` extra var set to `"service_account"` when
`client_id` is defined, otherwise `"basic"`.

---

### OpenShift or Kubernetes API Bearer Token (`kubernetes_bearer_token`)

Provides authentication for Kubernetes and OpenShift modules.

**Namespace:** `kubernetes_bearer_token` | **Kind:** `kubernetes` | **Entry point group:** `awx_plugins.managed_credentials`

**Injection method:** `custom_injectors` Python function

| Input Field | Label | Type | Secret | Injection |
|---|---|---|---|---|
| `host` | OpenShift or Kubernetes API Endpoint | string | no | Env: `K8S_AUTH_HOST` |
| `bearer_token` | API authentication bearer token | string | yes | Env: `K8S_AUTH_API_KEY` |
| `verify_ssl` | Verify SSL | boolean | no | Env: `K8S_AUTH_VERIFY_SSL` (`"True"` or `"False"`, default: `true`) |
| `ssl_ca_cert` | Certificate Authority data | string | yes | Written to temp file; env: `K8S_AUTH_SSL_CA_CERT` (only when `verify_ssl` is true) |

**Required:** `host`, `bearer_token`

---

### Container Registry (`registry`)

Provides authentication for pulling Execution Environment container images.

**Namespace:** `registry` | **Kind:** `registry` | **Entry point group:** `awx_plugins.managed_credentials`

**Injection method:** Consumed by AWX internally (not injected into Ansible)

| Input Field | Label | Type | Secret |
|---|---|---|---|
| `host` | Authentication URL | string | no (default: `"quay.io"`) |
| `username` | Username | string | no |
| `password` | Password or Token | string | yes |
| `verify_ssl` | Verify SSL | boolean | no (default: `true`) |

**Required:** `host`

Used in `build_execution_environment_params()` to pass
`container_auth_data` to ansible-runner for authenticating container image
pulls. Not injected into playbook runs.

---

### Ansible Galaxy/Automation Hub API Token (`galaxy_api_token`)

Provides authentication for Galaxy and Automation Hub servers.

**Namespace:** `galaxy_api_token` | **Kind:** `galaxy` | **Entry point group:** `awx_plugins.managed_credentials`

**Injection method:** Hardcoded in `RunProjectUpdate.build_env()`

| Input Field | Label | Type | Secret |
|---|---|---|---|
| `url` | Galaxy Server URL | string | no |
| `auth_url` | Auth Server URL | string | no |
| `token` | API Token | string | yes |

**Required:** `url`

During project updates, Galaxy credentials from the organization are iterated
and set as:
- `ANSIBLE_GALAXY_SERVER_SERVER<n>_URL`
- `ANSIBLE_GALAXY_SERVER_SERVER<n>_TOKEN`
- `ANSIBLE_GALAXY_SERVER_SERVER<n>_AUTH_URL`
- `ANSIBLE_GALAXY_SERVER_LIST`

---

### GitHub Personal Access Token (`github_token`)

Provides authentication for GitHub webhook status callbacks.

**Namespace:** `github_token` | **Kind:** `token` | **Entry point group:** `awx_plugins.managed_credentials.supported`

**Injection method:** Consumed by AWX internally (not injected into Ansible)

| Input Field | Label | Type | Secret |
|---|---|---|---|
| `token` | Token | string | yes |

**Required:** `token`

Used to post job status updates back to GitHub via the API.

---

### GitLab Personal Access Token (`gitlab_token`)

Provides authentication for GitLab webhook status callbacks.

**Namespace:** `gitlab_token` | **Kind:** `token` | **Entry point group:** `awx_plugins.managed_credentials.supported`

**Injection method:** Consumed by AWX internally (not injected into Ansible)

| Input Field | Label | Type | Secret |
|---|---|---|---|
| `token` | Token | string | yes |

**Required:** `token`

Used to post job status updates back to GitLab via the API.

---

### Bitbucket Data Center HTTP Access Token (`bitbucket_dc_token`)

Provides authentication for Bitbucket Data Center webhook status callbacks.

**Namespace:** `bitbucket_dc_token` | **Kind:** `token` | **Entry point group:** `awx_plugins.managed_credentials.supported`

**Injection method:** Consumed by AWX internally (not injected into Ansible)

| Input Field | Label | Type | Secret |
|---|---|---|---|
| `token` | Token | string | yes |

**Required:** `token`

Used to post job status updates back to Bitbucket Data Center via the API.

---

### GPG Public Key (`gpg_public_key`)

Provides a GPG public key for content signature validation during project
updates.

**Namespace:** `gpg_public_key` | **Kind:** `cryptography` | **Entry point group:** `awx_plugins.managed_credentials.supported`

**Injection method:** Hardcoded in `RunProjectUpdate`

| Input Field | Label | Type | Secret |
|---|---|---|---|
| `gpg_public_key` | GPG Public Key | string | yes |

**Required:** `gpg_public_key`

Passed as the `gpg_pubkey` extra var during project updates for content
signature validation.

---

### Terraform Backend Configuration (`terraform`)

Provides backend configuration for Terraform modules.

**Namespace:** `terraform` | **Kind:** `cloud` | **Entry point group:** `awx_plugins.managed_credentials.supported`

**Injection method:** `custom_injectors` Python function

| Input Field | Label | Type | Secret | Injection |
|---|---|---|---|---|
| `configuration` | Backend configuration | string | yes | Written to temp file; env: `TF_BACKEND_CONFIG_FILE` |
| `gce_credentials` | Google Cloud Platform account credentials | string | yes | Written to temp file; env: `GOOGLE_BACKEND_CREDENTIALS` (optional) |

**Required:** `configuration`

---

### HCP Terraform (`hcp_terraform`)

Provides authentication for HCP Terraform (Terraform Cloud).

**Namespace:** `hcp_terraform` | **Kind:** `cloud` | **Entry point group:** `awx_plugins.managed_credentials.supported`

**Injection method:** `custom_injectors` Python function

| Input Field | Label | Type | Secret | Injection |
|---|---|---|---|---|
| `hostname` | Hostname | string | no | Env: `TF_HOSTNAME` (default: `"app.terraform.io"`) |
| `token` | API Token | string | yes | Env: `TF_TOKEN` |

**Required:** `token`
