# Production signing keyring release

`src/lecturecast/signing-keyring.json` is the offline trust root for paid
Director Manifests. It contains public Ed25519 keys only. Private seeds belong
exclusively to `lecturecast-server` operations and must never enter this repo.

An empty keyring is allowed only during pre-release development, before the
first production key is approved. It makes Director Manifest verification fail
closed and cannot pass the release check. Every customer-facing release must
instead package exactly one `current` `lecturecast-prod-*` key; inspect the
actual release state with:

```bash
python scripts/update_signing_keyring.py --check
```

The command validates and prints the packaged keyring, which is the source of
truth across initial publication and later rotations. Fixture keys live only
under `tests/fixtures/` and are injected by tests; a release keyring rejects
fixture/test IDs.

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

Publish a normal Public release containing the new public key before the Server
worker starts using its matching private seed. There is no fixed waiting period,
signed attestation, or separate activation gate. During the internal canary,
verify one real Manifest with the released client; that proves the packaged
`current` key matches the production signer.

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
