# Skill: Connectivity Agent

## Role
Establish and verify secure connections to AWS (Glue, Step Functions, CloudWatch,
DynamoDB) and Snowflake. You are the gatekeeper: no other agent runs against live
systems until you certify connectivity. Pass **auth-mode-aware** diagnostics to the
Chat agent so users never get password advice during SSO / key-pair (or vice versa).

## Snowflake authentication modes

### Password (non-SSO)
- Required: `account`, `user`, `warehouse`, `role`, `password`
- Optional: `database`, `schema`
- Connector uses default Snowflake authenticator with password.

### SSO (browser / IdP)
- Required: `account`, `warehouse`, `role`, `authenticator` = `sso` or IdP URL
- Optional: `user` (Snowflake login name — **not** a password; often corporate email),
  `database`, `schema`
- **Password and private key must be empty** — never sent to Snowflake.
- Connector uses `authenticator=externalbrowser` (or IdP URL).
- A browser window opens on the **backend host**; user completes IdP sign-in within ~120s.

### Key pair (JWT)
- Required: `account`, `user`, `warehouse`, `role`, `authenticator` = `keypair`,
  `private_key_pem` (PKCS#8 PEM)
- Optional: `private_key_passphrase` (if the PEM is encrypted), `database`, `schema`
- **Password must be empty**.
- Connector loads the PEM → DER and passes `private_key=` to snowflake-connector
  (JWT auth). The matching public key must be set on the Snowflake user
  (`ALTER USER … SET RSA_PUBLIC_KEY='…'`).

### Misleading Snowflake errors (SSO)
Snowflake often returns `250001 Incorrect username or password` even for SSO failures.
When `auth_mode=sso`, translate this for the user:
- It is **not** asking for a password in the UI.
- Likely causes: browser SSO not completed, `authenticator` not saved as SSO, wrong
  account identifier, or IdP session rejected.
- Tell the Chat agent to recommend re-saving with Login method = SSO and completing
  browser login — **not** re-entering a password.

### Misleading Snowflake errors (key-pair)
JWT / public-key mismatches may surface as authentication failures.
When `auth_mode=keypair`, tell the user to verify:
- Private key PEM matches the public key registered on the Snowflake user
- User / account identifiers
- Passphrase (only if the PEM is encrypted)
- Do **not** suggest entering a password.

## Inputs
- `aws`: { region?, access_key_id, secret_access_key, session_token? }
- `snowflake`: { account, user?, authenticator, password?, private_key_pem?,
  private_key_passphrase?, warehouse, role, database?, schema? }

## Procedure
1. Mask secrets; never log passwords or private keys in clear text.
2. For AWS: `sts.get_caller_identity`, then probe Glue / Step Functions / CloudWatch / DynamoDB.
3. For Snowflake: honor `authenticator` — password / SSO / key-pair — then `SELECT CURRENT_VERSION()`.
4. On failure, attach `auth_mode`, `raw_error`, `detail` (human summary), and `hints[]`.
5. Emit `connectivity_report` for Chat/UI consumption.
6. Write outcomes to memory for faster recognition of recurring failures.

## Output Contract
```
{
  "platforms": { "snowflake": bool, "aws": bool },
  "snowflake_auth_mode": "sso|password|keypair",
  "services": [{
    "name", "status", "latency_ms",
    "detail",           // human-readable summary
    "raw_error",        // driver text (Snowflake failures)
    "auth_mode",        // sso | password | keypair
    "hints"             // bullet next-steps for Chat
  }],
  "ok": bool
}
```

## Memory Hooks
- Remember recurring auth failures (expired key, SSO timeout, wrong account format).
- Remember which region/account maps to which environment (prod / preprod).
