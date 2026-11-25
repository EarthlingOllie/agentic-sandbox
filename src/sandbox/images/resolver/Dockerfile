# The sandbox resolver: dnsmasq, answering lookups of set hostnames from the developer's
# machine by forwarding them to Docker's own DNS. It carries no traffic.
#
# Same base as the default agent image, so a machine that has run a set already has it.
FROM ubuntu:24.04

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends dnsmasq-base \
    && rm -rf /var/lib/apt/lists/*

COPY dnsmasq.conf /etc/dnsmasq.conf

# In the foreground, logging to stderr; sandbox appends `--server=/<domain>/127.0.0.11`.
ENTRYPOINT ["dnsmasq", "--keep-in-foreground", "--log-facility=-", "--conf-file=/etc/dnsmasq.conf"]
