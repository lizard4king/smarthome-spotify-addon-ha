"""Verify Cloudflare Access application identities; never trust email headers."""

from __future__ import annotations

import re


class IdentityError(ValueError):
    """A public, data-free authentication failure."""

    def __init__(self, code="identity_required", status=401):
        super().__init__(code)
        self.code = code
        self.status = status


class CloudflareIdentity:
    """Validate a signed application token against one configured team and app.

    PyJWT is imported only when this optional server feature is configured.
    The key URL is derived from a restricted hostname, never from token input.
    """

    def __init__(self, team_domain, audience, *, key_client=None):
        if not isinstance(team_domain, str) or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com",
                team_domain):
            raise ValueError("Ungültige Cloudflare-Team-Domain.")
        if not isinstance(audience, str) or not re.fullmatch(r"[a-f0-9]{64}", audience):
            raise ValueError("Ungültige Cloudflare-App-Audience.")
        try:
            import jwt
        except ImportError as error:
            raise ValueError("Für Benutzerverwaltung fehlt finance-control[server-auth].") from error
        self.jwt = jwt
        self.issuer = f"https://{team_domain}"
        self.audience = audience
        self.keys = key_client if key_client is not None else jwt.PyJWKClient(
            f"{self.issuer}/cdn-cgi/access/certs", timeout=5,
            cache_jwk_set=True, lifespan=300)

    def verify(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 16384:
            raise IdentityError()
        try:
            header = self.jwt.get_unverified_header(token)
            if (header.get("alg") != "RS256" or not isinstance(header.get("kid"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", header["kid"])):
                raise IdentityError()
            signing_key = self.keys.get_signing_key_from_jwt(token).key
            claims = self.jwt.decode(
                token, signing_key, algorithms=["RS256"], issuer=self.issuer,
                audience=self.audience,
                options={"require": ["iss", "aud", "exp", "iat", "nbf", "sub", "email", "type"]})
            subject, email = claims["sub"], claims["email"]
            if (claims["type"] != "app" or claims.get("common_name")
                    or not isinstance(subject, str) or not 1 <= len(subject) <= 256
                    or any(ord(char) < 33 for char in subject)
                    or not isinstance(email, str) or not 3 <= len(email) <= 254
                    or not email.isascii() or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
                raise IdentityError()
            return {"sub": subject, "email": email.casefold()}
        except self.jwt.PyJWKClientConnectionError:
            raise IdentityError("identity_provider_unavailable", 503) from None
        except self.jwt.PyJWTError:
            raise IdentityError() from None


def configured_identity(options):
    """No configuration preserves the local cockpit; partial setup fails closed."""
    fields = ("cloudflare_team_domain", "cloudflare_audience", "administrator_email")
    values = [options.get(field, "") for field in fields]
    if not any(values):
        return None, ()
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("Cloudflare-Team, App-Audience und Administrator-E-Mail vollständig setzen.")
    team, audience, email = (value.strip() for value in values)
    if not email.isascii() or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
        raise ValueError("Ungültige Administrator-E-Mail.")
    return CloudflareIdentity(team, audience), (email.casefold(),)
