# AAP Secret Passing to Ansible

We need a way to tell Ansible what values are secret/sensitive as marked by the AAP job template/host configuration. Some of this could potentially be automatically inferred by marking plugin options or known sensitive input values (like `-k`/`-K`) as an input value but AAP requires ways of marking any arbitrary variable as a secret regardless of whether they are used in a plugin or sensitive area known to Ansible. Also relying on pre-existing plugin configuration would require 3rd party plugins in collections outside of Ansible's control to be updated to reflect this new metadata.

Ultimately this means we need some way of communicating what these sensitive values are to Ansible so they can be marked as a secret and masked throughout the lifetime of the process. Some potential options, in no particular order, for doing this are:

1. Add new CLI or configuration option
2. Use vaulted file with `--extra-vars`
3. Use reserved variable name and provide through inventory

## 1. CLI/Config Option

One possible solution would be to either add a new CLI option like `--secrets-file` or use a new configuration entry where a file containing a list of known secrets could be loaded at startup. The format of this file is undefined right now but it will allow AAP to build up a list of known secret values from all of the various inputs, build this ephemeral file and provide it through this CLI/config option.

### Pros

+ Clear purpose, end users don't need to know what is happening under the covers
+ No extra encryption/vaulting of data needed at runtime

### Cons

+ Requires changes in `ansible`, `ansible-runner` (for CLI opt), and AAP
+ Using CLI option would break if used with older version, config option would be silently ignored
+ Potential exposure of secrets through ephemeral file during process lifetime, could we use pipes or inherited fds with a once off read?

## 2. Vaulted File Variable

A slight modification of the above would be to have AAP or `ansible-runner` generate a vaulted file that contains a list of values to mark as a secret. This vaulted file would be provided through `--extra-vars` in the CLI. This will take advantage that all vaulted values are marked as a secret and have Ansible register them at load time.

### Pros

+ Little to no changes required to `ansible`, no new APIs
+ Little to no changes required to `ansible-runner`, currently supports vaulted files and vault passwords
+ Data is encrypted at rest during the process runtime
+ This vaulted file contents can be used on older Ansible versions as a no-op

### Cons

+ Extra overhead at runtime to generate the vaulted file
+ Unknown questions as to whether creating a vaulted file can be done through a public API outside of the CLI
+ Potential exposure of secrets in `hostvars`/`vars` for older Ansible versions

## 3. Registered Variable Name

A slight modification of the above where a known variable, like `ansible_secrets` that could be provided through any existing variable mechanism. This could be a vaulted file like the above option but could also just be through any inventory data or `--extra-vars` value.

As an example, AAP could modify its inventory script to provide this under the `all` vars. Current the inventory script is something like

```python
 #! /usr/bin/env python3

# -*- coding: utf-8 -*-

print('inventory json')
```

This script could be customised to read the secrets from some ephemeral source and append the secrets under the `all` or a special inventory host used for this purpose. As it is a script it has no restrictions on how and where it reads this data from so it does not even need to be embedded in the script.

### Pros

+ More flexible than a vault as it could be loaded as part of the inventory

### Cons

+ Requires `ansible` to register a known variable name
+ I'm unsure if AAP or `ansible-runner` generates this script, if it's the latter then the API surface is not ideal
+ Older Ansible versions won't break but now these secrets could be accessible as a normal variable

## Rejected Options

A rejected option would be to automatically template sensitive values with the `| register_secret` filter. This is not ideal as:

+ Templates are lazily loaded only when referenced
+ A special variable name would have to be used (option 3) to make it unlazy which could be used instead of a filter per value
+ Values are meant to be untrusted and using a template becomes a security risk
+ Doesn't easily support env vars or other config options outside of variables
