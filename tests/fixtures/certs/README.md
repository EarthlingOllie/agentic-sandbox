Test-only certificates for the unit tests (`tests/fakes_certs.py`), generated with openssl.
Their keys protect nothing: the CA key was discarded and every name is under `.test`.

- `caroot/rootCA.pem`: a stand-in mkcert CA (valid for 100 years).
- `jira-123.pem`: a `*.jira-123.test` leaf signed by it (100 years).
- `jira-123-other-ca.pem`: the same name, signed by a different CA.
- `jira-123-short.pem`: the same name and CA, valid for one day from generation (so
  already expired, or within the reissue margin).
