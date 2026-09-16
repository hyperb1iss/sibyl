"""Validated, session-independent ownership of buffered mutations."""

from typing import Any
from uuid import UUID


def normalize_replay_identity(value: object) -> dict[str, Any] | None:
    """Accept only the complete server-issued v1 identity contract."""
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        return None
    if value["version"] != 1:
        return None
    identity: dict[str, Any] = {"version": 1}
    for key in ("server_instance_id", "user_id", "organization_id"):
        try:
            identity[key] = str(UUID(str(value[key])))
        except (KeyError, ValueError, TypeError):
            return None
    credential = value.get("credential")
    if not isinstance(credential, dict) or credential.get("kind") not in ("session", "api_key"):
        return None
    normalized: dict[str, Any] = {"kind": credential["kind"]}
    key_id = credential.get("api_key_id")
    if credential["kind"] == "api_key":
        try:
            key_id = str(UUID(str(key_id)))
        except (ValueError, TypeError):
            return None
    elif key_id is not None:
        return None
    normalized["api_key_id"] = key_id
    for key in ("scopes", "project_ids", "memory_space_ids", "memory_scope_keys"):
        if key not in credential:
            return None
        entries = credential[key]
        if entries is None and key != "scopes":
            normalized[key] = None
        elif isinstance(entries, list) and all(isinstance(item, str) for item in entries):
            normalized[key] = sorted(set(entries))
        else:
            return None
    identity["credential"] = normalized
    return identity


def pending_identity_matches(
    item: dict[str, Any],
    identity: dict[str, Any] | None,
    replay_scope: str | None,
    *,
    cached_identity: dict[str, Any] | None = None,
) -> bool:
    """Decide whether this login may replay a buffered write.

    A verified identity from the destination is the only proof that outranks
    credential lineage, so it is used alone whenever the server supplies one.

    `cached_identity` is the owner this login recorded locally, and it is
    consulted only when the destination cannot verify identities at all, which
    means a server without /auth/replay-identity. There, refusing every write
    that carries an owner would refuse every write on that server, since a
    buffered write now always records one. The lineage still has to agree, so
    this is no weaker than the credential-scope check such a server got before
    owners were recorded: the write must carry the owner this login holds and
    the scope that buffered it.
    """
    if item.get("replay_identity") is not None:
        owner = normalize_replay_identity(item["replay_identity"])
        if owner is None:
            return False
        if identity is not None:
            return owner == identity
        return (
            cached_identity is not None
            and owner == cached_identity
            and replay_scope is not None
            and item.get("replay_scope") == replay_scope
        )
    return replay_scope is not None and item.get("replay_scope") == replay_scope


def stored_replay_identity(
    api_url: str,
    *,
    credential_scope: str | None = None,
    access_token: str | None = None,
) -> dict[str, Any] | None:
    """Return the owner a stored login already proved, with no network call.

    A fresh `sibyl auth login` deletes this cache (auth_store.set_tokens), so
    whatever survives here was verified by the credential lineage that is still
    signed in. That makes it a sound owner to stamp on a write buffered while
    the server is unreachable, which is exactly when the identity endpoint
    cannot be asked.

    Pass `access_token` to require that the credential asking is the one the
    owner was recorded for. Without it, a login as somebody else between two
    reads could pair this caller's token with the new user's identity, and the
    write would replay into their organization.
    """
    from sibyl_cli.auth_store import read_server_credentials

    try:
        creds = read_server_credentials(api_url, credential_scope=credential_scope)
    except (OSError, RuntimeError, ValueError):
        return None
    if access_token is not None and creds.get("access_token") != access_token:
        return None
    return normalize_replay_identity(creds.get("pending_replay_identity"))


def warm_pending_replay_identity(
    api_url: str,
    access_token: str,
    *,
    credential_scope: str | None = None,
    insecure: bool = False,
    timeout: float = 10.0,
) -> bool:
    """Record the queue owner for a credential lineage that just began.

    A login is the one moment the CLI is certainly online, and it is also the
    moment `set_tokens` clears the previous owner. Asking for the identity now
    means a write buffered later, with the server unreachable, still knows who
    owns it; without this, the first offline write after a login is stranded
    the next time the login rotates. Best-effort: a login never fails because
    the queue's bookkeeping could not be updated.
    """
    import httpx

    from sibyl_cli.auth_store import cache_pending_replay_identity

    try:
        response = httpx.get(
            f"{api_url.rstrip('/')}/auth/replay-identity",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
            verify=not insecure,
        )
        if response.status_code != 200:
            return False
        identity = normalize_replay_identity(response.json())
    except Exception:
        return False
    if identity is None:
        return False
    try:
        return cache_pending_replay_identity(
            api_url,
            access_token,
            identity,
            credential_scope=credential_scope,
        )
    except (OSError, RuntimeError, ValueError):
        return False


def current_pending_owner(
    context_name: str | None = None,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Resolve this command's queue destination and owner from local state only.

    Used by the reporting surfaces, which run after every command and must not
    add a request. A base URL of None means the destination could not be
    resolved, and callers then decline to classify anything as foreign.

    The context resolves the way `get_client` resolves it, including the
    --context override, SIBYL_CONTEXT, and the directory pin. Resolving it any
    other way makes the triage disagree with the client about which server is
    current, and then a write for the server the operator selected reads as
    foreign and invites a discard.
    """
    from sibyl_cli.auth_store import normalize_api_url
    from sibyl_cli.client_transport import (
        _auth_credential_scope,
        _load_default_auth_token,
        _load_default_replay_scope,
        resolve_api_base_url,
    )

    try:
        if context_name is None:
            from sibyl_cli.client import resolve_client_context_name

            context_name = resolve_client_context_name()
        base_url = normalize_api_url(resolve_api_base_url(context_name))
        credential_scope_name = _auth_credential_scope(context_name)
        auth_token = _load_default_auth_token(base_url, credential_scope_name)
        replay_scope = _load_default_replay_scope(base_url, credential_scope_name, auth_token)
        identity = stored_replay_identity(
            base_url,
            credential_scope=credential_scope_name,
            access_token=auth_token,
        )
    except Exception:
        return (None, None, None)
    return (base_url or None, replay_scope, identity)
