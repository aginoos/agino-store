# Test signing key

`test-ed25519.pem` and `test-ed25519.pub` are used ONLY by the unit and integration tests.
They are never installed and no package trusts this key. Do not use it for anything real.

Generated with:

```sh
openssl genpkey -algorithm ed25519 -out test-ed25519.pem
openssl pkey -in test-ed25519.pem -pubout -outform DER | tail -c 32 | base64 -w0 > test-ed25519.pub
echo >> test-ed25519.pub
```

`.pub` is the 32-byte raw public key in base64 on one line (the format of design.md 4.2).
`tests/data/catalog/` is signed with this key by `tools/make-catalog.py`.
