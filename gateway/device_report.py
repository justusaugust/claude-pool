"""One report shape for every device step, so add, check and the tests read the same lines."""
from typing import NamedTuple, Optional

STATUSES = ('ok', 'missing', 'manual', 'skipped', 'failed', 'unreachable')
BLOCKING = {'missing', 'failed', 'unreachable'}
ADD_ORDER = CHECK_NAMES = ('reachable', 'native-claude', 'install', 'version', 'linger', 'shell', 'accounts', 'service', 'doctor',
                           'native-login', 'herdr', 'herdr-sessions', 'app', 'cua', 'conductor', 'readings', 'preview')


class Step(NamedTuple):
    name: str
    status: str
    detail: str
    fix: Optional[str] = None


def render(device, steps):
    lines = [device]
    for step in steps:
        lines.append(f'  {step.status:<11} {step.name:<13} {step.detail}')
        if step.fix: lines.append(f'      fix: {step.fix}')
    manual = [step for step in steps if step.status == 'manual']
    if manual:
        lines += ['', 'Needs you:'] + [f'  {step.name}: {step.fix}' for step in manual]
    return '\n'.join(lines)


def ready(steps):
    return not any(step.status in BLOCKING for step in steps)
