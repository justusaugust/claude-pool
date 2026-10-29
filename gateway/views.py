"""Small terminal views of sanitized gateway state."""
import textwrap
from datetime import datetime
from zoneinfo import ZoneInfo
from brake import describe
from telemetry import coverage_note
from settings import OPTIONS


def duration(seconds):
    minutes = max(0, round(seconds / 60))
    if minutes < 60: return str(minutes) + 'm'
    if minutes < 1440: return f'{minutes // 60}h {minutes % 60:02}m'
    return f'{minutes // 1440}d {(minutes % 1440) // 60}h'


def usage(data, *, width=80, color=False):
    width = max(20, min(width, 96))
    now = data['updatedAt']
    forecast = data['forecast']
    lines = []

    def paint(text, code):
        return f'\033[{code}m{text}\033[0m' if color else text

    def add(text='', code=None):
        # Provider labels must not become terminal control sequences.
        text = ''.join(c if c.isprintable() else ' ' for c in str(text))
        for line in textwrap.wrap(text, width=width - 2) or ['']:
            lines.append('  ' + (paint(line, code) if code else line))

    def band(pct):
        return '31' if pct >= 95 else '33' if pct >= 80 else '32'

    def quota(label, window, missing='not reported'):
        label_width = max(5, len(label))
        size = max(4, min(24, width - label_width - 17))
        if not window:
            track, value = '┄' * size, missing
            code = '90'
        else:
            pct = max(0, min(100, window['pct']))
            filled = max(1, int(pct * size / 100)) if pct else 0
            track = '━' * filled + '─' * (size - filled)
            value = f"{window['pct']:3.0f}%"
            code = band(window['pct'])
        reset = 'resets in ' + duration((window['resetsAt'] - now) / 1000) if window and window.get('resetsAt') else None
        suffix = '  ' + paint(reset, '90') if reset and width >= 64 else ''
        if 2 + label_width + 1 + len(track) + 2 + len(value) > width:
            add(label + ' ' + track, code)
            add('  ' + value, code)
        else:
            lines.append('  ' + f'{label:<{label_width}} ' + paint(track, code) + '  ' + paint(value, code) + suffix)
        if reset and width < 64:
            add('      ' + reset, '90')

    add('Claude pool', '1')
    add(f"● {data['ready']} / {len(data['accounts'])} account{'s' if len(data['accounts']) != 1 else ''} ready", '32' if data['ready'] else '33')
    add('Week = all models · Fable = separate weekly limit', '90')
    add('Not reported = Anthropic has not supplied a reading.', '90')
    if coverage_note(data['accounts']): add('Partial forecast · weekly all-model readings missing.', '33')
    if data.get('sessions'):
        held = sum(bool(s.get('brakeWaiting')) for s in data['sessions'])
        add(f"{len(data['sessions'])} connected/recent session(s) · {sum(s['busy'] for s in data['sessions'])} working"
            + (f' · {held} waiting for the brake' if held else ''), '90')
    add('─' * (width - 2), '90')
    for account in data['accounts']:
        status = account['status']
        code = '32' if status == 'ready' else '31' if status == 'limited' else '33'
        source = str(account.get('readingSource') or '').partition(':')[2]
        add(account['label'] + ' · ' + status + (' · reserved from ' + str(account['reserveAt']) + '%' if account.get('reserveAt') else '') + (' · reading from ' + source if source else ''), '1;' + code)
        age = duration((now - account['seenAt']) / 1000) + ' ago' if account['seenAt'] else 'unknown'
        add((account.get('plan') or '') + ' · checked ' + age, '90')
        quota('5h', account.get('fiveHour'))
        quota('Week', account.get('sevenDay'))
        weekly = account.get('quotaLimits', {}).get('sevenDay', {})
        if weekly.get('status') == 'rejected':
            add('Weekly limit reached' + (' · resets in ' + duration((weekly['resetsAt'] - now) / 1000) if weekly.get('resetsAt') else ' · reset not reported'), '31')
        quota('Fable', account.get('fableWeekly'), 'not reported' if 'fableWeekly' in account else 'unavailable')
        speed = next((r for r in forecast.get('accounts', []) if r['name'] == account.get('name') or r['label'] == account['label']), None)
        if speed and speed.get('windows'):
            rates = []
            for key, label in [('fiveHour', '5h'), ('sevenDay', 'week')]:
                window = speed['windows'].get(key)
                if not window: continue
                rate = window.get('ratePerHour')
                value = f'~{rate:.1f}%/h' if rate and rate > 0 else window['reason']
                rates.append(label + ' ' + value)
            add('Pace · ' + ' · '.join(rates), '90')
        add()

    add('─' * (width - 2), '90')
    add('Pool forecast · current quota', '1')
    if not forecast['eligibleAccounts']:
        add('No ready accounts confirmed', '33')
    elif forecast['seconds'] is None:
        reasons = {a['reason'] for a in forecast.get('accounts', []) if a.get('status') == 'ready'}
        if reasons == {'no recent burn'}:
            add('No recent burn measured', '36')
        elif reasons == {'reading stale'}:
            add('Waiting for fresh readings', '36')
        else:
            add('Collecting pace', '36')
        add('Needs 10m and 1% observed burn.', '90')
    else:
        add('~' + duration(round(forecast['seconds'] / 300) * 300) + ' at recent pace', '1;36')
        count, total = forecast['estimatedAccounts'], forecast['eligibleAccounts']
        quality = ' · pace ' + forecast['paceQuality'] if forecast.get('paceQuality') else ''
        add(f'{count}/{total} ready accounts estimated' + (' · partial' if forecast['partial'] else '') + quality, '90')
        low, high = forecast.get('secondsLow'), forecast.get('secondsHigh')
        if low is not None and high is not None and high - low > max(600, forecast['seconds'] * .15):
            add('Allowance range · ~' + duration(round(low / 300) * 300) + '–' + duration(round(high / 300) * 300), '90')
        elif low is not None and high is None:
            add('Allowance range not settled yet.', '90')
    brake, hold = forecast.get('brake') or {}, data.get('brake') or {}
    if brake.get('stage'):
        add('Brake · ' + describe(brake['stage']), '33')
    if hold.get('hold'):
        count = hold.get('waiting') or 0
        add('Background sessions wait' + (f" · {count} request{'s' if count != 1 else ''}" if count else ''), '33')
        if hold.get('calmSince'):
            add('Brake off for ' + duration((now - hold['calmSince']) / 1000) + '; background work goes after 15m', '90')
    if forecast['partial']:
        add('Unmeasured accounts/windows excluded.', '90')
    note = coverage_note(data['accounts'])
    if note: add(note, '33')
    if any(a['status'] == 'ready' and not a.get('fiveHour') for a in data['accounts']):
        add('5h readings missing; capacity may run out sooner.', '33')
    add('All-model windows only. Fable weekly is separate.', '90')
    add(f"Current quota to {forecast.get('threshold', 95):g}% used; pace can change.", '90')
    if forecast.get('resetBeforeLimit'):
        add('Resets arrive before projected depletion.', '90')
    if forecast['nextReset']:
        reset = forecast['nextReset']
        add()
        add('↻ Next reset · ' + reset['account'] + ' ' + reset['window'] + ' in ' + duration((reset['at'] - now) / 1000))
    renewal = forecast.get('renewals')
    if renewal:
        add('24h outlook · ~' + duration(renewal['coveredSeconds']) + ' projected coverage' + (' · partial' if renewal['partial'] else ''), '90')
        add('First known resets only; later renewals unknown.', '90')
    if data.get('persistenceError'):
        add('Warning: recovery state could not be saved.', '33')
    if data.get('recovery', {}).get('restoredBackup'):
        add('Recovery state restored from its last good backup.', '33')
    add()
    count = len(data['history'])
    add(f"{count} switch{'es' if count != 1 else ''} retained · claude history", '90')
    return '\n'.join(lines)


def history(data):
    labels = {a['name']: a['label'] for a in data['accounts']}
    timezone = OPTIONS.get('timezone') or str(datetime.now().astimezone().tzinfo)
    lines = ['Claude pool · recent switches (' + timezone + ')', '']
    for event in data['history'][:20]:
        when = (datetime.fromtimestamp(event['at'] / 1000, ZoneInfo(OPTIONS['timezone'])) if OPTIONS.get('timezone') else datetime.fromtimestamp(event['at'] / 1000).astimezone()).strftime('%d %b %H:%M')
        lines.append(f"{when}  {labels.get(event['from'], event['from'])} → {labels.get(event['to'], event['to'])} · {event['reason']}")
        cache = f"  cache read {event.get('cacheReadTokens', '?')} · cache created {event.get('cacheCreationTokens', '?')} tokens"
        lines.append(cache + (' · thinking dropped' if event.get('thinkingDropped') else ''))
    if not data['history']: lines.append('No account switches recorded yet.')
    return '\n'.join(lines)
