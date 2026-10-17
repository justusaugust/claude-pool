"""The Native Claude login of this machine.

`claude-native auth status --json` prints one object. Confirmed key names (2026-10-17): analyticsDisabled,
apiProvider, authMethod, configDirectory, email, loggedIn, orgId, orgName, projectsDirectory,
subscriptionType. The account email is `email`, the organization name is `orgName`.
"""
import json


def parse_auth_status(text):
    """{'email', 'organization'} of the signed-in Native login, or None when nobody is signed in."""
    try: value = json.loads(text)
    except ValueError: return None
    if not isinstance(value, dict) or value.get('loggedIn') is not True: return None
    email, organization = value.get('email'), value.get('orgName')
    if not isinstance(email, str) or not email: return None
    return {'email': email, 'organization': organization if isinstance(organization, str) else ''}
