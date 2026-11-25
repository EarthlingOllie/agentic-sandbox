# 01: The domain is configurable

**What to build:** A developer can choose the domain every set hostname ends in. With `domain: sandbox.test` in the config, a set's services answer to `<name>.<set>.sandbox.test` inside the set, the set's wildcard certificate covers that domain, and a web app is served at `https://<repo>.<set>.sandbox.test`. Addressing is otherwise unchanged (still the loopback model). Spec stories 21, 38, 40.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] A `domain` config key exists, defaulting to `test`; unset behaves exactly as today
- [ ] A value that is not a valid DNS suffix, and `local`, fail config validation with a field-level error
- [ ] Set hostnames, the names on the set network, extra hosts, the certificate, `APP_URL` and `VITE_HMR_HOST` all use the configured domain
- [ ] A set whose certificate was issued for another domain gets a new one on the next `up`
- [ ] `status` and `urls` (and their `--json`) show hostnames under the configured domain
- [ ] Unit tests cover the default and a multi-label domain through the CLI seam; an integration test serves a web app under a non-default domain
