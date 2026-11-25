# 01: The domain is configurable

**What to build:** A developer can choose the domain every set hostname ends in. With `domain: sandbox.test` in the config, a set's services answer to `<name>.<set>.sandbox.test` inside the set, the set's wildcard certificate covers that domain, and a web app is served at `https://<repo>.<set>.sandbox.test`. Addressing is otherwise unchanged (still the loopback model). Spec stories 21, 38, 40.

**Blocked by:** None (can start immediately)

**Status:** done

- [x] A `domain` config key exists, defaulting to `test`; unset behaves exactly as today
- [x] A value that is not a valid DNS suffix, and `local`, fail config validation with a field-level error
- [x] Set hostnames, the names on the set network, extra hosts, the certificate, `APP_URL` and `VITE_HMR_HOST` all use the configured domain
- [x] A set whose certificate was issued for another domain gets a new one on the next `up`
- [x] `status` and `urls` (and their `--json`) show hostnames under the configured domain
- [x] Unit tests cover the default and a multi-label domain through the CLI seam; an integration test serves a web app under a non-default domain

**Notes from the build:**

- The hosts block validation and the root-owned `sandbox-hosts` helper learned the domain
  (triage caveat): `sandbox-hosts write` takes `--domain`, passed only for a non-default
  domain, so a helper installed before this change keeps working for `test`. A machine that
  uses another domain through the helper (no `hosts_file`) must rerun `sudo sandbox setup` to
  install the new helper. The helper still refuses any name outside `<label>.<set>.<domain>`
  and any address outside the set's loopback /24; all of this goes away in 07.
- `local` and any domain ending in `.local` are refused (mDNS answers the whole suffix).
- The unit-test certificate fixtures were regenerated (their CA key had been discarded) to add
  a `*.jira-123.sandbox.test` leaf signed by the same stand-in CA.
- Evidence: `tests/unit/test_config.py` (key, default, field-level errors),
  `tests/unit/test_domain.py` (hostnames, network names, extra hosts, certificate and its
  reissue, `APP_URL`, `VITE_HMR_HOST`, `status`, `urls` and their `--json` under
  `sandbox.test`), `tests/unit/test_hosts.py` and `test_hosts_helper.py` (hosts block),
  `tests/integration/test_web.py::test_web_app_is_served_under_a_non_default_domain`.
