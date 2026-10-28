"""Generate realm-it-copilot.json from config/dev_users.yaml so dev and OIDC modes share one directory.

Passwords are `<username>-pass` (local sandbox only). Re-run after editing dev_users.yaml:
    python deploy/keycloak/generate_realm.py
"""
import json
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
users = yaml.safe_load((ROOT / "config/dev_users.yaml").read_text(encoding="utf-8"))["users"]

def attribute(name, display):
    return {"name": name, "displayName": display, "permissions": {"view": ["admin", "user"], "edit": ["admin"]}, "multivalued": False}

profile = {
    "attributes": [
        {"name": "username", "displayName": "${username}", "validations": {"length": {"min": 3, "max": 255}},
         "permissions": {"view": ["admin", "user"], "edit": ["admin"]}, "multivalued": False},
        {"name": "email", "displayName": "${email}", "validations": {"email": {}}, "permissions": {"view": ["admin", "user"], "edit": ["admin"]}, "multivalued": False},
        attribute("firstName", "${firstName}"), attribute("lastName", "${lastName}"),
        # Identity claims the API trusts; only realm admins may edit them.
        attribute("employee_id", "Employee ID"), attribute("department", "Department"),
    ],
}

def claim_mapper(attr):
    return {"name": attr, "protocol": "openid-connect", "protocolMapper": "oidc-usermodel-attribute-mapper",
            "config": {"user.attribute": attr, "claim.name": attr, "jsonType.label": "String", "access.token.claim": "true",
                       "id.token.claim": "true", "userinfo.token.claim": "true"}}

realm = {
    "realm": "it-copilot", "enabled": True, "sslRequired": "none", "accessTokenLifespan": 900, "registrationAllowed": False,
    "roles": {"realm": [{"name": r, "description": d} for r, d in (("employee", "Requests access for own account"),
              ("approver", "Decides access approvals"), ("it_admin", "Operates IT-Copilot"), ("auditor", "Reads traces and audit logs"))]},
    "clients": [{
        "clientId": "it-copilot-web", "name": "IT-Copilot Web", "publicClient": True, "standardFlowEnabled": True,
        # Password grant only so scripts can obtain sandbox tokens; disable in any shared deployment.
        "directAccessGrantsEnabled": True,
        "redirectUris": ["http://127.0.0.1:8080/*", "http://localhost:8080/*", "http://127.0.0.1:5173/*", "http://localhost:5173/*"],
        "webOrigins": ["+"], "attributes": {"pkce.code.challenge.method": "S256", "post.logout.redirect.uris": "+"},
        "protocolMappers": [
            {"name": "audience", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
             "config": {"included.custom.audience": "it-copilot", "access.token.claim": "true", "id.token.claim": "false"}},
            claim_mapper("employee_id"), claim_mapper("department"),
            {"name": "roles", "protocol": "openid-connect", "protocolMapper": "oidc-usermodel-realm-role-mapper",
             "config": {"claim.name": "roles", "multivalued": "true", "jsonType.label": "String", "access.token.claim": "true", "id.token.claim": "false"}},
        ],
    }],
    "users": [{
        "username": u["username"], "enabled": True, "email": f"{u['username']}@example.com", "emailVerified": True,
        "firstName": u["username"].title(), "lastName": "Demo",
        "attributes": {"employee_id": [u["employee_id"]], "department": [u["department"]]},
        "credentials": [{"type": "password", "value": f"{u['username']}-pass", "temporary": False}],
        "realmRoles": u["roles"],
    } for u in users],
    "components": {"org.keycloak.userprofile.UserProfileProvider": [
        {"providerId": "declarative-user-profile", "subComponents": {}, "config": {"kc.user.profile.config": [json.dumps(profile)]}}]},
}

out = pathlib.Path(__file__).with_name("realm-it-copilot.json")
out.write_text(json.dumps(realm, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"wrote {out} ({len(users)} users)")
