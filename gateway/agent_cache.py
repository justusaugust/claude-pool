"""Background agents (subagents and workflow agents) keep their prompt cache: one-hour cache writes, and their account.

Measured 22.-29.10.2026: Claude Code writes a subagent's cache for five minutes (a main session's for an hour). 65 % of the
subagents' cache-write spend rebuilt a prefix after a pause over five minutes, 25 % after the agent moved to another account.

Claude Code's background-agent labeler sends every 30 seconds per background agent the agent's whole transcript as class
`auxiliary` with the agent's own id. It reads the agent's cache, so it goes to the agent's account; it keeps its five-minute
writes (a read of the agent's one-hour entry costs the same, a miss costs 1.25x instead of 2x) and never pins the agent.
Measured 29./30.10.2026 while only the agents kept their account: 413 $ of labeler cache writes on other accounts in 15 hours.
"""
import re
from routing import eligible

FOLLOWS = {'auxiliary'}   # request classes that go to their agent's account but neither change cache nor pin the agent
HOUR = {'type': 'ephemeral', 'ttl': '1h'}
LIMIT = 4096
AGENT, ACCOUNT = re.compile(r'[A-Za-z0-9_-]{1,80}'), re.compile(r'[a-z0-9-]{1,80}')


def one_hour(request):
    """Makes every cache breakpoint (request, system, tools, message blocks, tool results) a one-hour write, in place, and
    returns how many changed. All of them change: the API refuses a one-hour breakpoint after a five-minute one."""
    blocks = [request]
    for key in ('system', 'tools'):
        if isinstance(request.get(key), list): blocks += request[key]
    for message in request['messages'] if isinstance(request.get('messages'), list) else []:
        content = message.get('content') if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            blocks.append(block)
            if isinstance(block, dict) and block.get('type') == 'tool_result' and isinstance(block.get('content'), list): blocks += block['content']
    changed = 0
    for block in blocks:
        mark = block.get('cache_control') if isinstance(block, dict) else None
        if isinstance(mark, dict) and {**mark, **HOUR} != mark:
            block['cache_control'] = {**mark, **HOUR}
            changed += 1
    return changed


class AgentAccounts:
    """The account that last answered each agent (x-claude-code-agent-id); the agents seen last, at most `limit`."""
    def __init__(self, limit=LIMIT):
        self.rows, self.limit = {}, limit

    def preferred(self, agent, accounts):
        """Its account while that account takes new work, else None: Shunt chooses as for any request without a preference."""
        name = self.rows.get(agent)
        account = next((a for a in accounts if name and a.get('name') == name), None)
        return name if account and eligible(account) else None

    def served(self, agent, account):
        if not isinstance(agent, str) or not AGENT.fullmatch(agent) or not isinstance(account, str) or not ACCOUNT.fullmatch(account): return
        self.rows.pop(agent, None)
        if len(self.rows) >= self.limit:
            for old in list(self.rows)[:max(1, self.limit // 4)]: del self.rows[old]
        self.rows[agent] = account
