"""The Native Claude login of this machine.

`claude-native auth status --json` prints one object. Confirmed key names (2026-10-17): analyticsDisabled,
apiProvider, authMethod, configDirectory, email, loggedIn, orgId, orgName, projectsDirectory,
subscriptionType. The account email is `email`, the organization name is `orgName`.
"""
import json


def parse_auth_status(text):
    """{'email', 'organization'} of the signed-in Native login; None only when the status says nobody is signed in.
    Raises ValueError for output it cannot read, so a caller never mistakes a broken status for "signed out"."""
    value = json.loads(text)  # ValueError on non-JSON
    if not isinstance(value, dict) or not isinstance(value.get('loggedIn'), bool): raise ValueError('Unreadable Native auth status')
    if not value['loggedIn']: return None
    email, organization = value.get('email'), value.get('orgName')
    if not isinstance(email, str) or not email: raise ValueError('Native auth status has no email')
    return {'email': email, 'organization': organization if isinstance(organization, str) else ''}
