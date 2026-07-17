# Production signing keyring release

`src/lecturecast/signing-keyring.json` is the offline trust root for paid
Director Manifests. It contains public Ed25519 keys only. Private seeds belong
exclusively to `lecturecast-server` operations and must never enter this repo.

Before the first production key is approved, the packaged keyring is
intentionally empty. Director Manifest verification therefore fails closed.
Fixture keys live only under `tests/fixtures/` and are injected by tests; a
release keyring rejects fixture/test IDs.

## Import a reviewed public envelope

The Server offline generation tool produces a public envelope containing a key,
validity window and SHA-256 fingerprint. Compare that fingerprint with the
out-of-band operations record, then run:

```bash
python scripts/update_signing_keyring.py \
  --entry /secure/release/manifest-signing-YYYYMM.public.json
python scripts/update_signing_keyring.py --check
pytest
python -m build
```

Import is atomic and append-only by key ID:

- one key is `current`;
- the former current key becomes `previous`;
- an existing key ID cannot change public bytes or validity window;
- previous public keys remain so archived Manifests continue to verify;
- only `lecturecast-prod-*` IDs can pass the release check.

Publish this Public release at least seven days before the Server worker starts
using the new private seed. Record the full Public Git commit, exact UTC
publication time and SHA-256 of the released wheel. At activation, operations
must download that exact customer-facing wheel and run the Server's offline
`lecturecast-signing-key public-first-check`; a local build is not release
evidence. The gate verifies that the wheel embeds this key as `current`, matches
the private seed and has aged for seven days before Director can be enabled.

## Revoke a compromised old key

A current key cannot be revoked until its replacement is imported. After the
replacement becomes current:

```bash
python scripts/update_signing_keyring.py --revoke <compromised-key-id>
python scripts/update_signing_keyring.py --check
```

Revocation marks the entry `revoked`; it does not delete it. Offline clients
cannot receive instant revocation, so the Director service must remain disabled
until the replacement Public release is available and old clients are rejected
by the capability handshake.

Never hand-edit a public key, key ID, status or window during release. Use the
tool, inspect the diff and require a human fingerprint review.
