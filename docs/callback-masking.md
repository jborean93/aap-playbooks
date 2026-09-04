# Callback Masking Compatibility

The new approach with secret masking is to mask the secrets at egress points of Ansible, e.g. in `display`. This is a change in how it works today where the raw module results could contain values that are masked. For example running a module like the below in `devel` right now would have this raw JSON result from the module and set as the task result:

```yaml
- uri:
    url: https://httpbin.org/basic-auth/username/password123
    url_username: username
    url_password: password123
  register: uri_result
```

```json
{
    ...
    // invocation only set if INJECT_INVOCATION is enabled
    "invocation": {
        "module_args": {
            ...
            "url": "https://httpbin.org/basic-auth/username/***********",
            "url_username": "username",
            "url_password": "VALUE_SPECIFIED_IN_NO_LOG_PARAMETER"
        },
    },
    "url": "https://httpbin.org/basic-auth/username/***********"
}
```

Anything that accesses this result (stdout or parsed dict), callbacks, action/connection plugins, will see that literal masked value `https://httpbin.org/basic-auth/username/***********` and not the actual password.

With the new secret masking the raw module JSON contains the literal secret values

```json
{
    ...
    "invocation": {
        "module_args": {
            ...
            "url": "https://httpbin.org/basic-auth/username/password123",
            "url_password": "password123",
            "url_username": "username",
        }
    },
    "url": "https://httpbin.org/basic-auth/username/password123"
}
```

The masking of these values only happens when something explicitly calls the `mask_secrets` API. For example the `display` class and its various methods will call this API for any input string leading to the following being emitted in stdout.

```json
{
    ...
    "invocation": {
        "module_args": {
            ...
            "url": "https://httpbin.org/basic-auth/username/<secret>",
            "url_password": "<secret>",
            "url_username": "username",
        }
    },
    "url": "https://httpbin.org/basic-auth/username/<secret>"
}
```

Anything that accesses this result, either as the raw stdout string or the parsed module result, has full access to the unredacted secrets.

Any plugin we have control over can easily be changed to either ensure they are using `display` or if expected, to mask the secrets at any of their egress points. The problem is third party plugins, specifically callback plugins that receive the task result as a dict which no longer contains the redacted values but the secrets which is a change in behaviour. Other plugins, like action, connection, etc, could also use the task result and log them somewhere but this is outside the scope of this problem as our proper interface for such logging and display tasks has always been callback plugins.

There are currently 147 callback plugins in the various collections and known Ansible content:

+ 5 are in Ansible
+ 1 is in `ansible-runner`
+ 9 are somewhat maintained by us
  + `ansible.posix` - 7
  + `sivel.toiletwater` - 2
+ 28 are in `community.general`

That still leaves around 100 other callback plugins that probably aren't easily changed to accomodate this new feature before the 2.22 release. Most are probably single user or limited in use and there are only 4 more plugins, excluding the 43 above, that are part of the community distribution of Ansible. It is unknown how many callback plugins are used internally by AAP users and how many of those remaining 100 use methods outside of `display`.

This change in behaviour and potential exposure of information that is currently redacted means we have to do one of the following:

1. Nothing, document this breaking change and tell 3rd party plugins to call `mask_secrets`
2. Add shim for `CallbackTaskResult` that can mask secrets manually when accessed/enumerated
3. Keep module heuristic masking

Option 2 and 3 will most likely be a short lived option that will either start as deprecated or quickly deprecated over a few releases. The end goal would be for callback plugins to either use `display` or manually mask the secrets if they wish.

## 1. Do Nothing

This is the simplest option as it is the current state. All callback plugins should either use `display` or mask the secrets themselves. The vast majority of users should be using the `default` callback or other callback plugins we control that can be updated by Ansible 2.22 is released.

### Pros

+ Simplest solution, no need for future deprecation or extra code
+ We could most likely change 95% of the callback plugins included in the `ansible` PyPI package before the 2.22 release
+ Callback plugins using `display` automatically get this masking

### Cons

+ This could be a security regression, people won't know unless they look at the logs and see sensitive values in there
+ There's no way to tell users that these secrets are no longer masked, like the above they may not realise something is wrong until they look at the logs
+ We don't know how many private callback plugins that are in use
+ We don't know how many of the above use a separate method for data egress outside of `display`
+ Customers may be surprised that previously sensitive data is no longer masked

# 2. CallbackTaskResult Shim

We can add a new attribute on the callback plugins that controls whether callback plugins accept the unredacted or redacted `CallbackTaskResult` shim. By default this attribute would mark the callback as requiring the shim. We could either create a custom enumerator for known problematic attributes like `warnings`, `result`, etc so that when the callback plugin accesses them, the shim will manually call `mask_secrets` on the value.

We would either mark this new attribute as deprecated immediately or after 1 or 2 versions and remove this shim after this deprecation period. This allows a small set of releases where older callback plugins that have not been updated to continue to see the redacted values while given them time to be updated before we remove the extra code.

### Pros

+ Preserves backwards compatbility for a few releases
+ Allows us to add a runtime warning if a callback does not opt into the new way of working
+ The redaction uses the newer masking methods, e.g. all controller secrets are masked
+ Plugins that want the unredacted result can opt into and just not mask the secrets

### Cons

+ More code complexity to an already complex plugin system
+ While the masking could be done lazily, it's extra work needed to be done on every string value in the task result dictionary

# 3. Keep Module Heuristic Masking

This will use the same type of attribute in option 2 but instead of adding a shim onto the `CallbackTaskResult` we instead use that attribute to see determine if the active callback plugins know and support the new way of working. If any callback plugin is enabled that has not set this flag we would communicate to the module to keep the module heuristic masking in the return result.

Like with option 2, this would be a temporary measure with this new attribute deprecated and only the new masking mechanism used in the future.

### Pros

+ Collections to continue to work as before, even the same masking format is used
+ No extra work needed to mask these results outside of the module doing what it does today
+ Code needed is somewhat simple, mostly just an extra flag with the existing heuristic code in the utils being preserved

### Cons

+ We need to continue to ship the heuristic code in the modules
  + I don't know if we still need to anyway as part of the module_utils API, `uri.py` uses `sanitize_keys`, maybe there are others
+ Extra complexity on the module side to swap between the two mechanisms
+ Only module's `no_log` secrets will be masked
  + This is a con compared to the other options, compared to devel today there is no change here
