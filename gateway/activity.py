"""Who started a main session's newest turn, from the request alone: a person (the owner or a partner) or an automatic sender.

The newest turn is everything after the last assistant message. It counts the user message's own text (string content or
text blocks; tool results, <system-reminder> meta and local command output do not count) and a message the owner typed
while the session worked: Claude Code 2.1.312 sends that as a system message after the tool result when
CLAUDE_CODE_FORCE_MID_CONVERSATION_SYSTEM is set (the pool sets it), otherwise inside a system reminder, both beginning
"The user sent a new message while you were working:". A text counts as a person's unless it matches a known automatic
sender: the Conductor Board's nudges, task notifications (except an `avd watch` line with a message from a person: an avd
name without a slash; agents are <person>/<agent>), what Claude Code marks as not typed by a person (system notifications,
non-user sources, scheduled tasks, plugin messages, Stop hook feedback), peer messages, subagent hand-backs, compaction
summaries, the pool's own Cockpit and respawn messages, and a scheduled wakeup whose prompt a ScheduleWakeup or CronCreate
call in the conversation set (Claude Code sends it unmarked). Skill bodies and image notes are no text. A false "person"
costs quota; a false "automatic" could hold up the owner, so unknown texts are a person's. Only the class leaves this
module, never text.
"""
import re

OWNER, AUTO = 'owner', 'auto'
PERSON = r'(?!dashboard )[a-z][a-z0-9_.-]*'   # an avd name without a slash; dashboard sends the security notes
TYPED = re.compile(r'The user sent a new message while you were working:\n(.*?)(?=\n\nThis is how Claude Code surfaces|</system-reminder>|\Z)', re.S)
REMINDER = re.compile(r'<system-reminder>.*?(?:</system-reminder>|\Z)', re.S)
META = re.compile(r'<(?:local-command-(?:stdout|stderr|caveat)|bash-(?:stdout|stderr))>|\[Image: |Base directory for this skill: '
                  r'|\(Re-invocation of /|Skill /\S+ was loaded earlier')
SCHEDULERS = {'ScheduleWakeup', 'CronCreate'}   # Claude Code tools whose `prompt` comes back later as a turn
BOARD = r'Conductor \(Board\): '
AUTOMATIC = re.compile('|'.join([
    # conductor/bin/conductor-board-server: REGELN texts (TEXT_STILL ... TEXT_AUFGABE), the plan reminder, the watcher restarts
    BOARD + r'Seit .{1,40}? kommt von dir nichts Neues', BOARD + r'Du scheinst dich im Kreis zu drehen',
    BOARD + r'Deine Arbeit ist .{1,40}? mit einem Verbindungsfehler', BOARD + r'Ich habe deinen hängenden Schritt abgebrochen',
    BOARD + r'Du bist neu gestartet', BOARD + r'Das Kontingent ist wieder frei', BOARD + r'Du stehst seit .{1,40}? still',
    BOARD + r'Neue Aufgabe aus dem Backlog', r'Conductor-Board: Dein Planstand ist von ',
    BOARD + r'Ich habe dich (?:frisch|neu) gestartet', BOARD + r'Dein Monitor auf wecker\.log fehlt', BOARD + r'Dein Neustart ist gescheitert',
    BOARD + r'Nach .{1,60}? fehlt dir der Monitor',
    # ~/.local/bin/conductor-abfrager (the Board's answer poller) and older quota nudges, as in the replay's rule
    r'Lade mit ToolSearch \(query select:Monitor,ArtifactData\)', r'Kontingent ist zurück', r'Das Kontingent ist wieder frei',
    # Claude Code (2.1.312): its marks for what no person typed, peer messages, subagent hand-backs, compaction, hooks
    r'\[(?:SYSTEM NOTIFICATION|MESSAGE FROM NON-USER SOURCE) - NOT USER INPUT\]', r'\[SCHEDULED TASK - AUTOMATED FIRING OF A CONFIGURED PROMPT\]',
    r'The \S{1,80} plugin sent a message', r'Another Claude session sent a message', r'A peer session sent a message',
    r'\[Subagent hand-back\] ', r'(?:<artifact-content-authored-by-others/>.{0,400}?)?This session is being continued from a previous conversation',
    r'Stop hook feedback:', r'Your claude\.ai usage limit has reset',
    # the pool's own Cockpit and respawn-continuation plugins
    r'Cockpit auto-wrap: ', r'Continue the existing task after this requested respawn\.',
]), re.S)
NOTIFICATION = re.compile(r'<task-notification>.*?(?:</task-notification>|\Z)', re.S)
# avd watch (ai-vaerk dashboard, brief.LivePrefix with MessageLine, AnswerLine, DecisionLine and HandoffLine)
AVD = re.compile(rf'^\s*(?:<event>)?\s*(?:\[(?:Nachricht|dringend)\] \[m\d+\] {PERSON} → |\[Antwort\] \[d\d+\] {PERSON} antwortet:'
                 rf'|\[dringend\] \[d\d+\] {PERSON} fragt |\[Übergeben\] \[[td]\d+\] {PERSON} übergibt an )', re.M)


def texts(messages):
    """The texts of the newest turn, oldest first."""
    found = []
    for message in reversed(messages):
        if not isinstance(message, dict): continue
        role, content = message.get('role'), message.get('content')
        if role == 'assistant': break
        if role not in ('user', 'system'): continue
        parts = [content] if isinstance(content, str) else [b['text'] for b in content if isinstance(b, dict) and b.get('type') == 'text'
                                                             and isinstance(b.get('text'), str)] if isinstance(content, list) else []
        for text in reversed(parts):
            here = [m.group(1) for m in TYPED.finditer(text)]
            if role == 'system':   # an avd watch line that arrived while the session worked
                here += [m.group(0) for m in NOTIFICATION.finditer(text) if AVD.search(m.group(0))]
            own = REMINDER.sub('', text).strip() if role == 'user' else ''
            if own and not META.match(own) and not TYPED.match(own): here.append(own)
            found += here[::-1]
    return found[::-1]


def scheduled(messages):
    """The prompts ScheduleWakeup and CronCreate calls in the conversation set: they come back as turns of their own."""
    prompts = set()
    for message in messages:
        content = message.get('content') if isinstance(message, dict) and message.get('role') == 'assistant' else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') in SCHEDULERS:
                prompt = (block.get('input') or {}).get('prompt') if isinstance(block.get('input'), dict) else None
                if isinstance(prompt, str) and prompt.strip(): prompts.add(prompt.strip())
    return prompts


def kind(text, prompts=()):
    text = text.strip()
    if NOTIFICATION.search(text[:1000]) and AVD.search(text): return OWNER   # an avd watch message from a person
    if text.startswith('<task-notification>') or AUTOMATIC.match(text) or text in prompts: return AUTO
    return OWNER


def classify(request):
    """OWNER when the newest turn holds a message from a person, AUTO when it holds only automatic ones, None when it holds
    none (a tool loop step)."""
    messages = request.get('messages') if isinstance(request, dict) else None
    if not isinstance(messages, list): return None
    found = texts(messages)
    prompts = scheduled(messages) if found else ()
    kinds = {kind(text, prompts) for text in found}
    return OWNER if OWNER in kinds else AUTO if kinds else None
