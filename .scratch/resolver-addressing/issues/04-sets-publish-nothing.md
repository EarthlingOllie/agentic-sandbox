# 04: Sets publish nothing; services answer on their conventional ports

**What to build:** The switch-over. A set no longer leases loopback IPs, publishes ports or writes a hosts block: each service is reached at its set hostname on its conventional port at the container's own address, so two sets both answer on 3306. `status` and `urls` show where things are now. Spec stories 1-8, 15-17, 19, 22, 23, 54-56, 58.

**Blocked by:** 02

**Status:** done

- [x] The set's override publishes no ports and resets every host-side binding a repo declares, including `0.0.0.0` ones
- [x] `up`, `down`, `prune` and `destroy` no longer lease or release IPs or write or remove a hosts block
- [x] Two sets running the same service both accept connections on its conventional port at their containers' addresses
- [x] A web app serves HTTPS on 443 at its container's address with the repo contract unchanged; `vite:` sets the HMR host and publishes nothing
- [x] `status` and `urls` show a running service's current address, read from Docker, and none for a stopped set; their JSON models make the address optional
- [x] `urls` lists each service's declared container ports as `host:port`; a service that declares none is listed without a port
- [x] `up`'s success output shows set hostnames and conventional ports
- [x] Services still reach each other by short name inside the set
- [x] Unit tests cover the override and the output models through the CLI seam; integration tests connect to two sets on one port and to a web app by address
