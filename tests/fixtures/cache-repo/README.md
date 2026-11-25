# cache-repo

Fixture repo for the walkthrough harness (copied into a repo named `cache`): a second backing
service with its own conventional port, `redis` on 6379, and `worker`, a service that
declares no ports and listens on 9000 all the same. See `.sandbox/compose.yaml`.
