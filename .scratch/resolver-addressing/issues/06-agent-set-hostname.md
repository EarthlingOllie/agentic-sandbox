# 06: The agent has a set hostname

**What to build:** The agent answers at `agent.<set>.<domain>`, from the developer's machine and from inside the set, so something the agent serves can be opened by name. Spec stories 13, 57.

**Blocked by:** 04

**Status:** ready-for-agent

- [ ] The agent carries `agent.<set>.<domain>` on the set network, including when an agent override is in use
- [ ] The name resolves through the resolver to the agent's address while the set runs
- [ ] `status` shows the agent's hostname and address; `urls` lists the agent
- [ ] The hostname uniqueness check still rejects a web app extra host named `agent`
- [ ] Unit tests cover the override and outputs; an integration test connects to a listener started in the agent by its hostname's address
