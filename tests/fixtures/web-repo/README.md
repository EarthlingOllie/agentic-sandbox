Fixture repo for the HTTPS web app integration tests: one `x-sandbox: {web: true}` service,
named after the repo (`web`), serving HTTPS on 443 as an unprivileged user. It declares
one extra host, `admin` (`https://admin.<set>.test`, same container and IP).
